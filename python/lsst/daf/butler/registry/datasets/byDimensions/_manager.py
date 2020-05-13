from __future__ import annotations

__all__ = ("ByDimensionsDatasetRecordStorageManager",)

from typing import (
    Dict,
    Iterable,
    Iterator,
    Optional,
    Tuple,
    TYPE_CHECKING,
)

import sqlalchemy

from lsst.daf.butler import (
    DatasetRef,
    DatasetType,
    ddl,
    DimensionGraph,
    DimensionUniverse,
)
from lsst.daf.butler.registry import ConflictingDefinitionError
from lsst.daf.butler.registry.interfaces import DatasetRecordStorage, DatasetRecordStorageManager

from .tables import makeStaticTableSpecs, addDatasetForeignKey, makeDynamicTableName, makeDynamicTableSpec
from ._storage import ByDimensionsDatasetRecordStorage

if TYPE_CHECKING:
    from lsst.daf.butler.registry.interfaces import (
        CollectionManager,
        Database,
        StaticTablesContext,
    )
    from .tables import StaticDatasetTablesTuple


class ByDimensionsDatasetRecordStorageManager(DatasetRecordStorageManager):
    """A manager class for datasets that uses one dataset-collection table for
    each group of dataset types that share the same dimensions.

    In addition to the table organization, this class makes a number of
    other design choices that would have been cumbersome (to say the least) to
    try to pack into its name:

     - It uses a private surrogate integer autoincrement field to identify
       dataset types, instead of using the name as the primary and foreign key
       directly.

     - It aggressively loads all DatasetTypes into memory instead of fetching
       them from the database only when needed or attempting more clever forms
       of caching.

    Alternative implementations that make different choices for these while
    keeping the same general table organization might be reasonable as well.

    Parameters
    ----------
    db : `Database`
        Interface to the underlying database engine and namespace.
    collections : `CollectionManager`
        Manager object for the collections in this `Registry`.
    static : `StaticDatasetTablesTuple`
        Named tuple of `sqlalchemy.schema.Table` instances for all static
        tables used by this class.
    """
    def __init__(self, *, db: Database, collections: CollectionManager, static: StaticDatasetTablesTuple):
        self._db = db
        self._collections = collections
        self._static = static
        self._byName = {}
        self._byId = {}

    @classmethod
    def initialize(cls, db: Database, context: StaticTablesContext, *, collections: CollectionManager,
                   universe: DimensionUniverse) -> DatasetRecordStorageManager:
        # Docstring inherited from DatasetRecordStorageManager.
        specs = makeStaticTableSpecs(type(collections), universe=universe)
        static = context.addTableTuple(specs)
        return cls(db=db, collections=collections, static=static)

    @classmethod
    def addDatasetForeignKey(cls, tableSpec: ddl.TableSpec, *, name: str = "dataset",
                             constraint: bool = True, onDelete: Optional[str] = None,
                             **kwargs) -> ddl.FieldSpec:
        # Docstring inherited from DatasetRecordStorageManager.
        return addDatasetForeignKey(tableSpec, name=name, onDelete=onDelete, constraint=constraint, **kwargs)

    def refresh(self, *, universe: DimensionUniverse):
        # Docstring inherited from DatasetRecordStorageManager.
        byName = {}
        byId = {}
        c = self._static.dataset_type.columns
        for row in self._db.query(self._static.dataset_type.select()).fetchall():
            name = row[c.name]
            dimensions = DimensionGraph.decode(row[c.dimensions_encoded], universe=universe)
            datasetType = DatasetType(name, dimensions, row[c.storage_class])
            dynamic = self._db.getExistingTable(makeDynamicTableName(datasetType),
                                                makeDynamicTableSpec(datasetType, type(self._collections)))
            storage = ByDimensionsDatasetRecordStorage(db=self._db, datasetType=datasetType,
                                                       static=self._static, dynamic=dynamic,
                                                       dataset_type_id=row["id"],
                                                       collections=self._collections)
            byName[datasetType.name] = storage
            byId[storage._dataset_type_id] = storage
        self._byName = byName
        self._byId = byId

    def find(self, name: str) -> Optional[DatasetRecordStorage]:
        # Docstring inherited from DatasetRecordStorageManager.
        return self._byName.get(name)

    def register(self, datasetType: DatasetType) -> Tuple[DatasetRecordStorage, bool]:
        # Docstring inherited from DatasetRecordStorageManager.
        storage = self._byName.get(datasetType.name)
        if storage is None:
            row, inserted = self._db.sync(
                self._static.dataset_type,
                keys={"name": datasetType.name},
                compared={
                    "dimensions_encoded": datasetType.dimensions.encode(),
                    "storage_class": datasetType.storageClass.name,
                },
                returning=["id"],
            )
            dynamic = self._db.ensureTableExists(
                makeDynamicTableName(datasetType),
                makeDynamicTableSpec(datasetType, type(self._collections)),
            )
            storage = ByDimensionsDatasetRecordStorage(db=self._db, datasetType=datasetType,
                                                       static=self._static, dynamic=dynamic,
                                                       dataset_type_id=row["id"],
                                                       collections=self._collections)
            self._byName[datasetType.name] = storage
            self._byId[storage._dataset_type_id] = storage
        else:
            if datasetType != storage.datasetType:
                raise ConflictingDefinitionError(f"Given dataset type {datasetType} is inconsistent "
                                                 f"with database definition {storage.datasetType}.")
            inserted = False
        if inserted and datasetType.isComposite:
            for component in datasetType.storageClass.components:
                self.register(datasetType.makeComponentDatasetType(component))
        return storage, inserted

    def __iter__(self) -> Iterator[DatasetType]:
        for storage in self._byName.values():
            yield storage.datasetType

    def getDatasetRef(self, id: int) -> Optional[DatasetRef]:
        # Docstring inherited from DatasetRecordStorageManager.
        sql = sqlalchemy.sql.select(
            [
                self._static.dataset.columns.dataset_type_id,
                self._static.dataset.columns[self._collections.getRunForeignKeyName()],
            ]
        ).select_from(
            self._static.dataset
        ).where(
            self._static.dataset.columns.id == id
        )
        row = self._db.query(sql).fetchone()
        if row is None:
            return None
        recordsForType = self._byId.get(row[self._static.dataset.columns.dataset_type_id])
        if recordsForType is None:
            self.refresh()
            recordsForType = self._byId.get(row[self._static.dataset.columns.dataset_type_id])
            assert recordsForType is not None, "Should be guaranteed by foreign key constraints."
        return DatasetRef(
            recordsForType.datasetType,
            dataId=recordsForType.getDataId(id=id),
            id=id,
            run=self._collections[row[self._collections.getRunForeignKeyName()]].name
        )

    def attachComponents(self, composites: Iterable[Tuple[DatasetRef, Dict[str, DatasetRef]]]
                         ) -> Iterator[DatasetRef]:
        # Docstring inherited from DatasetRecordStorageManager.
        rows = []
        results = []
        for parentRef, components in composites:
            rows.extend(
                {
                    "component_name": componentName,
                    "component_dataset_id": componentRef.getCheckedId(),
                    "parent_dataset_id": parentRef.getCheckedId(),
                    "simple": (parentRef.dataId == componentRef.dataId and parentRef.run == componentRef.run)
                }
                for componentName, componentRef in components.items()
            )
            results.append(parentRef.resolved(parentRef.id, parentRef.run, components=components))
        self._db.insert(self._static.dataset_composition, *rows)
        yield from results

    def fetchComponents(self, ref: DatasetRef) -> DatasetRef:
        # Docstring inherited from DatasetRecordStorageManager.
        sql = sqlalchemy.sql.select(
            [
                self._static.dataset_composition.columns.component_dataset_id.label("id"),
                self._static.dataset_composition.columns.component_name.label("name"),
                self._static.dataset_composition.columns.simple.label("simple"),
            ]
        ).select_from(
            self._static.dataset_composition
        ).where(
            self._static.dataset_composition.columns.parent_dataset_id == ref.getCheckedId()
        )
        components = {}
        for row in self._db.query(sql).fetchall():
            if row["simple"]:
                datasetType = ref.datasetType.makeComponentDatasetType(row["name"])
                components[row["name"]] = DatasetRef(datasetType, ref.dataId, id=row["id"], run=ref.run)
            else:
                components[row["name"]] = self.getDatasetRef(row["id"])
        return ref.resolved(id=ref.id, run=ref.run, components=components)