from __future__ import annotations

from abc import abstractmethod
from datetime import datetime
from typing import (
    Any,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
)

import sqlalchemy

from .core.schema import TableSpec
from .core.dimensions import (
    DataCoordinate,
    ExpandedDataCoordinate,
    DimensionElement,
    DimensionGraph,
    DimensionRecord,
)
from .core.datasets import DatasetType
from .core.run import Run
from .core.quantum import Quantum
from .core.utils import NamedKeyDict
from .core.queries import CollectionsExpression

from .iterables import DataIdIterable, SingleDatasetTypeIterable, DatasetIterable

from .backend import (
    GeneralRecordStorage,
    DatasetRecordStorage,
    DatasetTypesExpression,
    DimensionRecordStorage,
    OpaqueRecordStorage,
    QuantumRecordStorage,
    RegistryBackend,
)


def _maybeUnion(tables: Iterable[sqlalchemy.sql.FromClause]) -> Optional[sqlalchemy.sql.FromClause]:
    tables = tuple(tables)
    if len(tables) == 0:
        return None
    elif len(tables) == 1:
        return tables[0]
    else:
        return sqlalchemy.sql.union_all(*tables)


class ChainedDimensionRecordStorage(DimensionRecordStorage):

    def __init__(self, chain: Iterable[DimensionRecordStorage]):
        self._chain = list(chain)

    def push(self, link: DimensionRecordStorage):
        self._chain.insert(0, link)

    def isWriteable(self) -> bool:
        return self._chain[0].isWriteable()

    def matches(self, dataId: Optional[ExpandedDataCoordinate] = None) -> bool:
        return any(link.matches(dataId) for link in self._chain)

    def insert(self, *records: DimensionRecord):
        return self._chain[0].insert(*records)

    def fetch(self, dataIds: DataIdIterable) -> Dict[DataCoordinate, DimensionRecord]:
        selectable = dataIds.select()
        missing = set(dataIds)
        fetched = {}
        for link in self._chain:
            recent = link.fetch(DataIdIterable(missing, flags=dataIds.flags, materialized=True,
                                               selectable=selectable))
            if recent:
                # After we've obtained (some) results from one link in the
                # chain, we stop passing in a SQLAlchemy object (if we ever
                # did).
                selectable = None
            missing -= recent.keys()
            fetched.update(recent)
        return fetched

    def select(self, dataId: Optional[ExpandedDataCoordinate] = None) -> Optional[sqlalchemy.sql.FromClause]:
        return _maybeUnion(link.select(dataId) for link in self._chain if link.matches(dataId))

    def selectSkyPixOverlap(self, dataId: Optional[ExpandedDataCoordinate] = None
                            ) -> Optional[sqlalchemy.sql.FromClause]:
        return _maybeUnion(link.selectSkyPixOverlap(dataId) for link in self._chain if link.matches(dataId))

    element: DimensionElement


DimensionRecordCache = NamedKeyDict[DimensionElement, Dict[DataCoordinate, DimensionRecord]]


class ChainedDatasetRecordStorage(DatasetRecordStorage):

    def __init__(self, chain: Iterable[DatasetRecordStorage]):
        self._chain = list(chain)

    def push(self, link: DimensionRecordStorage):
        self._chain.insert(0, link)

    def isWriteable(self) -> bool:
        return self._chain[0].isWriteable()

    def matches(self, collections: CollectionsExpression = ...,
                dataId: Optional[ExpandedDataCoordinate] = None) -> bool:
        return any(link.matches(collections, dataId) for link in self._chain)

    def insert(self, run: Run, dataIds: DataIdIterable, *, producer: Optional[Quantum] = None
               ) -> SingleDatasetTypeIterable:
        return self._chain[0].insert(run, dataIds, producer=producer)

    def find(self, collections: List[str], dataIds: DataIdIterable = None) -> SingleDatasetTypeIterable:
        assert dataIds.dimensions == self.datasetType.dimensions
        selectable = dataIds.select()
        found = {}
        missing = set(dataIds)
        # Search each collection separately, because we need to return the
        # dataset from the first collection in which it appears for each
        # data ID.
        for collection in collections:
            for link in self._chain:
                if not link.matches([collection]):
                    continue
                iterable = link.find(
                    [collection],
                    DataIdIterable(missing, flags=dataIds.flags, materialized=True, selectable=selectable),
                )
                # Put the results in a dict, which might consume iterable's
                # iterator.
                recent = {dataset.dataId: dataset for dataset in iterable}
                if recent:
                    # After we've obtained (some) results from one link in the
                    # chain, we stop passing in a SQLAlchemy object (if we ever
                    # did).
                    selectable = None
                missing -= recent.keys()
                if not found and not missing:
                    # We found all of the data IDs in just one link, so we can
                    # pass along any SQLAlchemy selectable returned from it.
                    return SingleDatasetTypeIterable(iterable.datasetType, recent.values(),
                                                     flags=iterable.flags, selectable=iterable.selectable,
                                                     reentrant=True)
                found.update(recent)
        # Results came from multiple links, so we can't pass along a
        # SQLAlchemy selectable.
        return SingleDatasetTypeIterable(iterable.datasetType, found.values(),
                                         flags=iterable.flags, reentrant=True)

    def delete(self, datasets: SingleDatasetTypeIterable):
        self._chain[0].delete(datasets)

    def associate(self, collection: str, datasets: SingleDatasetTypeIterable, *,
                  begin: Optional[datetime] = None, end: Optional[datetime] = None):
        self._chain[0].associate(collection, datasets, begin=begin, end=end)

    def disassociate(self, collection: str, datasets: SingleDatasetTypeIterable):
        self._chain[0].disassociate(collection, datasets)

    def select(self, collections: CollectionsExpression = ...,
               dataId: Optional[ExpandedDataCoordinate] = None,
               isResult: bool = True, addRank: bool = False) -> Optional[sqlalchemy.sql.FromClause]:
        return _maybeUnion(link.select(collections, dataId) for link in self._chain
                           if link.matches(collections, dataId))

    datasetType: DatasetType


class ChainedOpaqueRecordStorage(OpaqueRecordStorage):

    def __init__(self, chain: Iterable[OpaqueRecordStorage]):
        self._chain = list(chain)

    def push(self, link: DimensionRecordStorage):
        self._chain.insert(0, link)

    def isWriteable(self) -> bool:
        return self._chain[0].isWriteable()

    def insert(self, *data: dict):
        self._chain[0].insert(*data)

    def fetch(self, **where: Any) -> Iterator[dict]:
        for link in self._chain:
            yield from link.fetch(**where)

    def delete(self, **where: Any):
        self._chain[0].delete(**where)

    name: str


class ChainedGeneralRecordStorage(GeneralRecordStorage):

    def __init__(self, chain: Iterable[GeneralRecordStorage]):
        self._chain = list(chain)

    def push(self, link: GeneralRecordStorage):
        self._chain.insert(0, link)

    def isWriteable(self) -> bool:
        return self._chain[0].isWriteable()

    def matches(self, collections: CollectionsExpression = ...,
                datasetTypes: DatasetTypesExpression = None) -> bool:
        return any(link.matches(collections, datasetTypes) for link in self._chain)

    def ensureRun(self, name: str) -> Run:
        return self._chain[0].ensureRun(name)

    def findRun(self, name: str) -> Optional[Run]:
        for link in self._chain:
            run = link.findRun(name)
            if run is not None:
                return run
        return None

    def updateRun(self, run: Run):
        self._chain[0].updateRun(run)

    def ensureCollection(self, name: str, *, calibration: bool = False):
        self._chain[0].ensureCollection(name, calibration=calibration)

    def insertDatasetLocations(self, datastoreName: str, datasets: DatasetIterable, *,
                               ephemeral: bool = False):
        self._chain[0].insertDatasetLocations(datastoreName, datasets, ephemeral=ephemeral)

    def fetchDatasetLocations(self, datasets: DatasetIterable) -> Iterator[str]:
        if len(self._chain) > 1:
            datasets = datasets.reentrant()
        for link in self._chain:
            yield from link.fetchDatasetLocations(datasets)

    def deleteDatasetLocations(self, datastoreName: str, datasets: DatasetIterable):
        self._chain[0].deleteDatasetLocations(datastoreName, datasets)

    def selectDatasetTypes(self, datasetTypes: DatasetTypesExpression = ..., *,
                           collections: CollectionsExpression = ...) -> sqlalchemy.sql.FromClause:
        return _maybeUnion(link.selectDatasetTypes(datasetTypes, collections) for link in self._chain
                           if link.matches(collections, datasetTypes))

    def selectCollections(self, collections: CollectionsExpression = ..., *,
                          datasetTypes: DatasetTypesExpression = ...) -> sqlalchemy.sql.FromCLause:
        return _maybeUnion(link.selectCollections(collections, datasetTypes) for link in self._chain
                           if link.matches(collections, datasetTypes))


class ChainedQuantumRecordStorage(QuantumRecordStorage):

    def insert(self, quantum: Quantum) -> Quantum:
        return self._chain[0].insert(quantum)

    def update(self, quantum: Quantum):
        self._chain[0].update(quantum)

    dimensions: DimensionGraph


class ChainedRegistryBackend(RegistryBackend):

    def __init__(self, *, write: bool = True):
        self._generalStorage: Optional[ChainedGeneralRecordStorage] = None
        self._opaqueStorage: Dict[str, OpaqueRecordStorage] = {}
        self._dimensionStorage: Dict[str, DimensionRecordStorage] = {}
        self._quantumStorage: Dict[DimensionGraph, QuantumRecordStorage] = {}
        self._datasetStorage: Dict[str, DatasetRecordStorage] = {}

    @abstractmethod
    def _makeOpaqueRecordStorage(self, name: str, spec: TableSpec, *, write: bool = True,
                                 ephemeral: bool = False) -> OpaqueRecordStorage:
        pass

    @abstractmethod
    def _makeDimensionRecprdStorage(self, element: DimensionElement, *, write: bool = True
                                    ) -> DimensionRecordStorage:
        pass

    @abstractmethod
    def _makeGeneralRecordStorage(self, *, write: bool = True) -> GeneralRecordStorage:
        pass

    @abstractmethod
    def _makeQuantumRecordStorage(self, dimensions: DimensionGraph, *, write: bool = True
                                  ) -> QuantumRecordStorage:
        pass

    @abstractmethod
    def _makeDatasetRecordStorage(self, datasetType: DatasetType, *, write: bool = True
                                  ) -> DatasetRecordStorage:
        pass

    def ensureWriteableGeneralRecordStorage(self):
        if not self._generalStorage.isWriteable():
            self._generalStorage.push(self._makeGeneralRecordStorage(write=True))

    def getGeneralRecordStorage(self) -> GeneralRecordStorage:
        return self._generalStorage

    def registerOpaqueData(self, name: str, spec: TableSpec, *, write: bool = True, ephemeral: bool = False
                           ) -> OpaqueRecordStorage:
        storage = self._opaqueStorage.get(name)
        if storage is None:
            link = self._makeOpaqueRecordStorage(name, spec, write=write, ephemeral=ephemeral)
            storage = ChainedOpaqueRecordStorage([link])
            self._opaqueStorage[name] = storage
        elif write and not storage.isWriteable():
            link = self._makeOpaqueRecordStorage(name, spec, write=write, ephemeral=ephemeral)
            storage.push(link)
        return storage

    def fetchOpaqueData(self, name: str) -> OpaqueRecordStorage:
        return self._opaqueStorage[name]

    def registerDimensionElement(self, element: DimensionElement, *, write: bool = True
                                 ) -> DimensionRecordStorage:
        storage = self._dimensionStorage.get(element.name)
        if storage is None:
            link = self._makeDimensionRecordStorage(element, write=write)
            storage = ChainedDimensionRecordStorage([link])
            self._dimensionStorage[element] = storage
        elif write and not storage.isWriteable():
            link = self._makeDimensionRecordStorage(element, write=write)
            storage.push(link)
        return storage

    def registerQuanta(self, dimensions: DimensionGraph, *, write: bool = True) -> QuantumRecordStorage:
        storage = self._quantumStorage.get(dimensions)
        if storage is None:
            link = self._makeQuantumRecordStorage(dimensions, write=write)
            storage = ChainedQuantumRecordStorage([link])
            self._quantumStorage[dimensions] = storage
        elif write and not storage.isWriteable():
            link = self._makeQuantumRecordStorage(dimensions, write=write)
            storage.push(link)
        return storage

    def registerDatasetType(self, datasetType: DatasetType, *, write: bool = True) -> DatasetRecordStorage:
        storage = self._datasetStorage.get(datasetType.name)
        if storage is None:
            link = self._makeQuantumRecordStorage(datasetType, write=write)
            storage = ChainedQuantumRecordStorage([link])
            self._datasetStorage[datasetType.name] = storage
        elif write and not storage.isWriteable():
            link = self._makeQuantumRecordStorage(datasetType, write=write)
            storage.push(link)
        return storage

    def fetchDatasetType(self, name: str) -> DatasetRecordStorage:
        return self._datasetStorage[name]
