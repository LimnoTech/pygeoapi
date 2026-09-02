# =================================================================
#
# Authors: Terence Tuhinanshu <ttuhinanshu@element84.com>
#
# Copyright (c) 2026 USGS Water Mission Area
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation
# files (the "Software"), to deal in the Software without
# restriction, including without limitation the rights to use,
# copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following
# conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
# OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.
#
# =================================================================

import logging

import shapely
from geoalchemy2.shape import to_shape
from sqlalchemy.orm import Session
from sqlalchemy.sql.expression import and_

from pygeoapi.provider.base import ProviderItemNotFoundError
from pygeoapi.provider.sql import PostgreSQLProvider

LOGGER = logging.getLogger(__name__)

#: STAC version to advertise when a row does not carry its own
DEFAULT_STAC_VERSION = '1.0.0'


class STACItemsProvider(PostgreSQLProvider):
    """
    Serve rows of a STAC Items table as valid STAC Item GeoJSON Features.

    The generic :class:`~pygeoapi.provider.sql.PostgreSQLProvider` flattens
    every table column into ``feature['properties']``. For a STAC Items table
    whose columns *are* the top-level fields of a STAC Item (``collection``,
    ``stac_version``, ``bbox``, ``assets``, ``links`` and a JSONB
    ``properties`` blob) that produces an invalid Item with buried assets and
    a doubly-nested properties bag. This provider reuses all of the parent's
    engine, reflection, filtering and paging machinery and overrides only:

    - :meth:`_sqlalchemy_to_feature` -- reshape a row into a STAC Item.
    - collection scoping -- when a ``collection`` is configured, restrict
      every query to that collection so a single ``stac_items`` table can
      back many single-collection pygeoapi resources.

    Provider definition keys (in addition to the PostgreSQL provider's):

    :collection: STAC collection id to scope this resource to (optional; when
                 omitted the provider serves every collection in the table)
    :collection_field: name of the column holding the collection id
                       (default ``collection``)
    """

    def __init__(self, provider_def: dict):
        """
        STACItemsProvider constructor

        :param provider_def: provider definition

        :returns: pygeoapi.provider.stac_sql.STACItemsProvider
        """
        self.collection_field = provider_def.get(
            'collection_field', 'collection')
        self.collection = provider_def.get('collection')
        super().__init__(provider_def)
        LOGGER.debug(f'Collection field: {self.collection_field}')
        LOGGER.debug(f'Collection scope: {self.collection}')

    def _sqlalchemy_to_feature(self, item, crs_transform_out=None,
                               select_properties=None):
        """
        Transform a reflected STAC Items row into a STAC Item GeoJSON Feature.

        :param item: SQLAlchemy result
        :param crs_transform_out: CRS transformation
        :param select_properties: ignored; STAC Items are whole documents

        :returns: `dict` of a STAC Item
        """
        if select_properties is None:
            select_properties = []

        item_dict = item.__dict__

        feature = {
            'type': 'Feature',
            'stac_version': item_dict.get('stac_version')
            or DEFAULT_STAC_VERSION,
            'id': item_dict[self.id_field],
            'properties': item_dict.get('properties') or {},
            'assets': item_dict.get('assets') or {},
            'links': item_dict.get('links') or [],
        }

        collection = item_dict.get(self.collection_field)
        if collection is not None:
            feature['collection'] = collection

        # Geometry conversion mirrors the parent provider so CRS handling
        # stays identical; the shapely geometry is reused for the bbox below.
        shapely_geom = None
        if item_dict.get(self.geom) is not None:
            wkb_geom = item_dict[self.geom]
            try:
                shapely_geom = to_shape(wkb_geom)
            except TypeError:
                shapely_geom = shapely.geometry.shape(wkb_geom)
            if crs_transform_out is not None:
                shapely_geom = crs_transform_out(shapely_geom)
            feature['geometry'] = shapely.geometry.mapping(shapely_geom)
        else:
            feature['geometry'] = None

        # Prefer the stored bbox; fall back to the geometry envelope.
        bbox = item_dict.get('bbox')
        if not bbox and shapely_geom is not None:
            bbox = list(shapely_geom.bounds)
        if bbox:
            feature['bbox'] = bbox

        return feature

    def _get_property_filters(self, properties):
        """
        Extend the parent property filters with the collection scope so that
        every :meth:`query` is confined to this resource's collection.

        :param properties: list of tuples (name, value)

        :returns: SQLAlchemy filter expression
        """
        filters = super()._get_property_filters(properties)

        if self.collection is None:
            return filters

        collection_column = getattr(self.table_model, self.collection_field)
        collection_filter = collection_column == self.collection

        # The parent returns ``True`` ("let everything through") when no
        # property filters are configured; avoid a redundant and_(True, ...).
        if filters is True:
            return collection_filter
        return and_(filters, collection_filter)

    def get(self, identifier, crs_transform_spec=None, **kwargs):
        """
        Query the provider for a specific item by id.

        For a collection-scoped resource, an id that resolves to a row in a
        different collection is treated as not found, and the prev/next links
        are confined to the configured collection.

        :param identifier: feature id
        :param crs_transform_spec: `CrsTransformSpec` instance, optional

        :returns: `dict` of a STAC Item
        """
        feature = super().get(
            identifier, crs_transform_spec=crs_transform_spec, **kwargs)

        if self.collection is None:
            return feature

        if feature.get('collection') != self.collection:
            msg = f'No such item: {self.id_field}={identifier}.'
            raise ProviderItemNotFoundError(msg)

        self._set_scoped_prev_next(feature, identifier)

        return feature

    def _set_scoped_prev_next(self, feature, identifier):
        """
        Overwrite the parent's prev/next (computed across the whole table)
        with the neighbours inside the configured collection.

        :param feature: `dict` of the STAC Item being returned
        :param identifier: feature id
        """
        id_column = getattr(self.table_model, self.id_field)
        collection_column = getattr(self.table_model, self.collection_field)

        with Session(self._engine) as session:
            prev_item = (
                session.query(self.table_model)
                .filter(collection_column == self.collection)
                .filter(id_column < identifier)
                .order_by(id_column.desc())
                .first()
            )
            next_item = (
                session.query(self.table_model)
                .filter(collection_column == self.collection)
                .filter(id_column > identifier)
                .order_by(id_column.asc())
                .first()
            )

        feature['prev'] = (
            getattr(prev_item, self.id_field)
            if prev_item is not None else identifier
        )
        feature['next'] = (
            getattr(next_item, self.id_field)
            if next_item is not None else identifier
        )
