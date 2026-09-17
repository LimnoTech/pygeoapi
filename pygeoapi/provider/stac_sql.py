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
from sqlalchemy.sql.expression import and_, or_

from pygeoapi.provider.base import ProviderItemNotFoundError
from pygeoapi.provider.sql import PostgreSQLProvider

LOGGER = logging.getLogger(__name__)

#: STAC version to advertise when a row does not carry its own
DEFAULT_STAC_VERSION = '1.0.0'

#: Recognised provider modes -- which flavour of STAC document the configured
#: table holds. Only ``items`` is implemented today; ``collections`` is a
#: planned mode (see the collections-search work) and is rejected until built.
SUPPORTED_MODES = ('items', 'collections')


class STACSQLProvider(PostgreSQLProvider):
    """
    Serve rows of a STAC SQL table as valid STAC documents.

    The generic :class:`~pygeoapi.provider.sql.PostgreSQLProvider` flattens
    every table column into ``feature['properties']``. For a STAC table whose
    columns *are* the top-level fields of a STAC document (``collection``,
    ``stac_version``, ``bbox``, ``assets``, ``links`` and a JSONB
    ``properties`` blob) that produces an invalid document with buried assets
    and a doubly-nested properties bag. This provider reuses all of the
    parent's engine, reflection, filtering and paging machinery and overrides
    only the seams needed to reshape rows into valid STAC.

    The ``mode`` definition key selects which STAC document the table holds:

    - ``items`` (default) -- reshape ``stac_items`` rows into STAC Items and,
      when a ``collection`` is configured, scope every query to it so a single
      ``stac_items`` table can back many single-collection resources.
    - ``collections`` -- reshape ``stac_collections`` rows into STAC
      Collections. ``datetime`` filtering becomes an interval overlap against
      the collection's ``start_datetime``/``end_datetime`` columns, and ``q``
      free-text search matches the ``title``/``description`` columns.

    Provider definition keys (in addition to the PostgreSQL provider's):

    :mode: ``items`` (default) or ``collections`` -- the kind of STAC document
           this table holds
    :collection: STAC collection id to scope an ``items`` resource to
                 (optional; when omitted the provider serves every collection
                 in the table)
    :collection_field: name of the column holding the collection id
                       (default ``collection``)
    :start_datetime_field: (``collections`` mode) name of the column holding
                           the extent start (default ``start_datetime``)
    :end_datetime_field: (``collections`` mode) name of the column holding the
                         extent end (default ``end_datetime``)
    """

    #: Internal ``properties`` key used to smuggle a free-text ``q`` term
    #: through :meth:`query` into :meth:`_get_property_filters` (which turns
    #: it into a title/description ``ILIKE``). Not a real column.
    _Q_SENTINEL = '__stac_q__'

    def __init__(self, provider_def: dict):
        """
        STACSQLProvider constructor

        :param provider_def: provider definition

        :returns: pygeoapi.provider.stac_sql.STACSQLProvider
        """
        self.mode = provider_def.get('mode', 'items')
        if self.mode not in SUPPORTED_MODES:
            raise ValueError(
                f'Unsupported STAC provider mode: {self.mode!r} '
                f'(expected one of {SUPPORTED_MODES})')

        self.collection_field = provider_def.get(
            'collection_field', 'collection')
        self.collection = provider_def.get('collection')
        self.start_datetime_field = provider_def.get(
            'start_datetime_field', 'start_datetime')
        self.end_datetime_field = provider_def.get(
            'end_datetime_field', 'end_datetime')
        super().__init__(provider_def)
        LOGGER.debug(f'Mode: {self.mode}')
        LOGGER.debug(f'Collection field: {self.collection_field}')
        LOGGER.debug(f'Collection scope: {self.collection}')

    def _sqlalchemy_to_feature(self, item, crs_transform_out=None,
                               select_properties=None):
        """
        Reshape a reflected row into the STAC document for this provider's
        mode: a STAC Item (``items``) or a STAC Collection (``collections``).

        :param item: SQLAlchemy result
        :param crs_transform_out: CRS transformation
        :param select_properties: ignored; STAC docs are whole documents

        :returns: `dict` of a STAC Item or Collection
        """
        if self.mode == 'collections':
            return self._row_to_collection(item)
        return self._row_to_item(item, crs_transform_out, select_properties)

    def _row_to_collection(self, item):
        """
        Transform a reflected ``stac_collections`` row into a STAC Collection.

        The derived query-acceleration columns (``geometry``, the datetime
        bounds, ``created_at``/``updated_at``) are dropped; the STAC top-level
        fields and any JSONB extension blobs are lifted to the top level.

        :param item: SQLAlchemy result

        :returns: `dict` of a STAC Collection
        """
        item_dict = item.__dict__

        collection = {
            'type': item_dict.get('type') or 'Collection',
            'stac_version': item_dict.get('stac_version')
            or DEFAULT_STAC_VERSION,
            'id': item_dict[self.id_field],
            'description': item_dict.get('description') or '',
            'links': item_dict.get('links') or [],
        }

        # STAC top-level fields, lifted verbatim when present.
        for key in ('title', 'license', 'extent', 'assets', 'summaries',
                    'stac_extensions', 'keywords', 'providers'):
            value = item_dict.get(key)
            if value is not None:
                collection[key] = value

        # Extension fields stored in their own JSONB columns.
        for key in ('sci:doi', 'cube:dimensions', 'cube:variables'):
            value = item_dict.get(key)
            if value is not None:
                collection[key] = value

        # A residual JSONB ``properties`` blob (if any) is merged in without
        # clobbering fields already set from dedicated columns.
        for key, value in (item_dict.get('properties') or {}).items():
            collection.setdefault(key, value)

        return collection

    def _row_to_item(self, item, crs_transform_out=None,
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
        Extend the parent property filters with this provider's extra scopes:
        the ``items`` collection scope and the ``collections`` free-text
        (``q``) search, either of which may be absent.

        :param properties: list of tuples (name, value); a
                           :attr:`_Q_SENTINEL` entry carries a free-text term

        :returns: SQLAlchemy filter expression
        """
        freetext = None
        passthrough = []
        for name, value in properties:
            if name == self._Q_SENTINEL:
                freetext = value
            else:
                passthrough.append((name, value))

        filters = super()._get_property_filters(passthrough)

        extra = []
        if freetext is not None:
            extra.append(self._freetext_clause(freetext))
        if self.collection is not None:
            collection_column = getattr(
                self.table_model, self.collection_field)
            extra.append(collection_column == self.collection)

        # The parent returns ``True`` ("let everything through") when no
        # property filters are configured; avoid a redundant and_(True, ...).
        if not extra:
            return filters
        if filters is not True:
            extra.insert(0, filters)
        return extra[0] if len(extra) == 1 else and_(*extra)

    def _freetext_clause(self, q):
        """
        Build a case-insensitive ``ILIKE`` over a collection's title or
        description for a free-text ``q`` term.

        :param q: free-text search term

        :returns: SQLAlchemy boolean expression
        """
        pattern = f'%{q}%'
        title = getattr(self.table_model, 'title')
        description = getattr(self.table_model, 'description')
        return or_(title.ilike(pattern), description.ilike(pattern))

    def get(self, identifier, crs_transform_spec=None, **kwargs):
        """
        Query the provider for a specific document by id.

        In ``collections`` mode the reshaped STAC Collection is returned as-is
        (the parent's item-oriented prev/next fields are dropped). In
        ``items`` mode, for a collection-scoped resource an id that resolves
        to a row in a different collection is treated as not found, and the
        prev/next links are confined to the configured collection.

        :param identifier: document id
        :param crs_transform_spec: `CrsTransformSpec` instance, optional

        :returns: `dict` of a STAC Item or Collection
        """
        feature = super().get(
            identifier, crs_transform_spec=crs_transform_spec, **kwargs)

        if self.mode == 'collections':
            feature.pop('prev', None)
            feature.pop('next', None)
            return feature

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

    def query(self, *args, q=None, **kwargs):
        """
        Query the table, adding ``collections`` mode's free-text search.

        In ``collections`` mode a ``q`` term is carried into
        :meth:`_get_property_filters` as a :attr:`_Q_SENTINEL` property, which
        turns it into a title/description ``ILIKE``; the parent runs the rest
        of the query unchanged. In ``items`` mode ``q`` passes straight
        through (the parent ignores it, as before).

        :param q: full-text search term(s)

        :returns: GeoJSON FeatureCollection
        """
        if q and self.mode == 'collections':
            properties = list(kwargs.pop('properties', None) or [])
            properties.append((self._Q_SENTINEL, q))
            kwargs['properties'] = properties
            q = None

        return super().query(*args, q=q, **kwargs)

    def _get_datetime_filter(self, datetime_):
        """
        Filter on the temporal extent.

        In ``collections`` mode a collection matches when its
        ``[start_datetime, end_datetime]`` extent *overlaps* the requested
        instant or interval, with a NULL bound treated as open (unbounded).
        In ``items`` mode this defers to the parent's single-column filter.

        :param datetime_: temporal instant or ``begin/end`` interval

        :returns: SQLAlchemy filter expression
        """
        if self.mode != 'collections':
            return super()._get_datetime_filter(datetime_)

        if datetime_ in (None, '../..'):
            return True

        start_col = getattr(self.table_model, self.start_datetime_field)
        end_col = getattr(self.table_model, self.end_datetime_field)

        if '/' in datetime_:
            lower, upper = datetime_.split('/')
            lower = None if lower == '..' else lower
            upper = None if upper == '..' else upper
        else:
            lower = upper = datetime_

        clauses = []
        if upper is not None:
            clauses.append(or_(start_col.is_(None), start_col <= upper))
        if lower is not None:
            clauses.append(or_(end_col.is_(None), end_col >= lower))

        if not clauses:
            return True
        return clauses[0] if len(clauses) == 1 else and_(*clauses)
