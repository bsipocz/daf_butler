# This file is part of daf_butler.
#
# Developed for the LSST Data Management System.
# This product includes software developed by the LSST Project
# (http://www.lsst.org).
# See the COPYRIGHT file at the top-level directory of this distribution
# for details of code ownership.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.


import astropy
from astropy.table import Table as AstropyTable
from astropy.utils.diff import report_diff_values
import io
import os


from .. import (
    Butler,
    Config,
    StorageClassFactory,
)
from ..tests import addDatasetType, MetricsExample
from ..registry import CollectionType


class ButlerTestHelper:
    """Mixin with helpers for unit tests."""

    def assertAstropyTablesEqual(self, tables, expectedTables):
        """Verify that a list of astropy tables matches a list of expected
        astropy tables.

        Parameters
        ----------
        tables : `astropy.table.Table` or iterable [`astropy.table.Table`]
            The table or tables that should match the expected tables.
        expectedTables : `astropy.table.Table`
                         or iterable [`astropy.table.Table`]
            The tables with expected values to which the tables under test will
            be compared.
        """
        # If a single table is passed in for tables or expectedTables, put it
        # in a list.
        if isinstance(tables, AstropyTable):
            tables = [tables]
        if isinstance(expectedTables, AstropyTable):
            expectedTables = [expectedTables]
        diff = io.StringIO()
        self.assertEqual(len(tables), len(expectedTables))
        for table, expected in zip(tables, expectedTables):
            # Assert that we are testing what we think we are testing:
            self.assertIsInstance(table, AstropyTable)
            self.assertIsInstance(expected, AstropyTable)
            # Assert that they match:
            self.assertTrue(report_diff_values(table, expected, fileobj=diff), msg="\n" + diff.getvalue())


def readTable(textTable):
    """Read an astropy table from formatted text.

    Contains formatting that causes the astropy table to print an empty string
    instead of "--" for missing/unpopulated values in the text table.


    Parameters
    ----------
    textTable : `str`
        The text version of the table to read.

    Returns
    -------
    table : `astropy.table.Table`
        The table as an astropy table.
    """
    return AstropyTable.read(textTable,
                             format="ascii",
                             fill_values=[("", 0, "")])


class MetricTestRepo:
    """Creates and manage a test repository on disk with datasets that
    may be queried and modified for unit tests.

    Parameters
    ----------
    root : `str`
        The location of the repository, to pass to ``Butler.makeRepo``.
    configFile : `str`
        The path to the config file, to pass to ``Butler.makeRepo``.
    """

    @staticmethod
    def _makeExampleMetrics():
        """Make an object to put into the repository.
        """
        return MetricsExample({"AM1": 5.2, "AM2": 30.6},
                              {"a": [1, 2, 3],
                               "b": {"blue": 5, "red": "green"}},
                              [563, 234, 456.7, 752, 8, 9, 27])

    @staticmethod
    def _makeDimensionData(id, name, datetimeBegin=None, datetimeEnd=None):
        """Make a dict of dimensional data with default values to insert into
        the registry.
        """
        data = dict(instrument="DummyCamComp",
                    id=id,
                    name=name,
                    physical_filter="d-r",
                    visit_system=1)
        if datetimeBegin:
            data["datetime_begin"] = datetimeBegin
            data["datetime_end"] = datetimeEnd
        return data

    def __init__(self, root, configFile):
        self.root = root
        Butler.makeRepo(self.root, config=Config(configFile))
        butlerConfigFile = os.path.join(self.root, "butler.yaml")
        self.storageClassFactory = StorageClassFactory()
        self.storageClassFactory.addFromConfig(butlerConfigFile)

        # New datasets will be added to run and tag, but we will only look in
        # tag when looking up datasets.
        run = "ingest/run"
        tag = "ingest"
        self.butler = Butler(butlerConfigFile, run=run, collections=[tag], tags=[tag])

        # Create and register a DatasetType
        self.datasetType = addDatasetType(self.butler, "test_metric_comp", ("instrument", "visit"),
                                          "StructuredCompositeReadComp")

        # Add needed Dimensions
        self.butler.registry.insertDimensionData("instrument", {"name": "DummyCamComp"})
        self.butler.registry.insertDimensionData("physical_filter", {"instrument": "DummyCamComp",
                                                                     "name": "d-r",
                                                                     "band": "R"})
        self.butler.registry.insertDimensionData("visit_system", {"instrument": "DummyCamComp",
                                                                  "id": 1,
                                                                  "name": "default"})
        visitStart = astropy.time.Time("2020-01-01 08:00:00.123456789", scale="tai")
        visitEnd = astropy.time.Time("2020-01-01 08:00:36.66", scale="tai")
        self.butler.registry.insertDimensionData("visit", dict(instrument="DummyCamComp",
                                                               id=423,
                                                               name="fourtwentythree",
                                                               physical_filter="d-r",
                                                               visit_system=1,
                                                               datetimeBegin=visitStart,
                                                               datetimeEnd=visitEnd))
        self.butler.registry.insertDimensionData("visit", dict(instrument="DummyCamComp",
                                                               id=424,
                                                               name="fourtwentyfour",
                                                               physical_filter="d-r",
                                                               visit_system=1))

        self.addDataset({"instrument": "DummyCamComp", "visit": 423})
        self.addDataset({"instrument": "DummyCamComp", "visit": 424})

    def addDataset(self, dataId, run=None, datasetType=None):
        """Create a new example metric and add it to the named run with the
        given dataId.

        Overwrites tags, so this does not try to associate the new dataset with
        existing tags. (If/when tags are needed this can be added to the
        arguments of this function.)

        Parameters
        ----------
        dataId : `dict`
            The dataId for the new metric.
        run : `str`, optional
            The name of the run to create and add a dataset to. If `None`, the
            dataset will be added to the root butler.
        datasetType : ``DatasetType``, optional
            The dataset type of the added dataset. If `None`, will use the
            default dataset type.
        """
        if run:
            self.butler.registry.registerCollection(run, type=CollectionType.RUN)
        metric = self._makeExampleMetrics()
        self.butler.put(metric,
                        self.datasetType if datasetType is None else datasetType,
                        dataId,
                        run=run,
                        tags=())
