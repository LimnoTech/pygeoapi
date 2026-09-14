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

# These are unit tests for the seams STACSQLProvider adds on top of
# PostgreSQLProvider -- the row->STAC-Item reshape, the collection scope, and
# the ``mode`` switch. They do not require a live database: the provider is
# instantiated without connecting (``__new__``) and the reshape/filter methods
# are exercised directly, while the mode guard runs before the parent
# constructor connects. End-to-end coverage against a real ``stac_items`` table
# belongs in an integration test alongside test_postgresql_provider.py.

import pytest
from sqlalchemy import Column, String
from sqlalchemy.orm import declarative_base

from pygeoapi.provider.stac_sql import STACSQLProvider, DEFAULT_STAC_VERSION


_Base = declarative_base()


class _FakeItems(_Base):
    """Minimal stand-in for the reflected stac_items model."""
    __tablename__ = 'stac_items'
    id = Column(String, primary_key=True)
    collection = Column(String)
    type = Column(String)


class _FakeCollections(_Base):
    """Minimal stand-in for the reflected stac_collections model."""
    __tablename__ = 'stac_collections'
    id = Column(String, primary_key=True)
    title = Column(String)
    description = Column(String)
    start_datetime = Column(String)
    end_datetime = Column(String)


class _Row:
    """Stand-in for a reflected SQLAlchemy row (attributes via __dict__)."""
    def __init__(self, **attrs):
        self.__dict__.update(attrs)


def _make_provider(collection=None):
    """Build a provider without running __init__ (no DB connection)."""
    provider = STACSQLProvider.__new__(STACSQLProvider)
    provider.mode = 'items'
    provider.id_field = 'id'
    provider.geom = 'geometry'
    provider.collection_field = 'collection'
    provider.collection = collection
    # get_table_model() is annotated `-> Table` but actually returns an
    # automapped ORM class (see sql.py); _FakeItems matches that runtime
    # shape, so silence the static-only mismatch against the Table annotation.
    provider.table_model = _FakeItems  # type: ignore[assignment]
    return provider


def _make_collections_provider():
    """Build a collections-mode provider without a DB connection."""
    provider = STACSQLProvider.__new__(STACSQLProvider)
    provider.mode = 'collections'
    provider.id_field = 'id'
    provider.geom = 'geometry'
    provider.collection_field = 'collection'
    provider.collection = None
    provider.start_datetime_field = 'start_datetime'
    provider.end_datetime_field = 'end_datetime'
    provider.table_model = _FakeCollections  # type: ignore[assignment]
    return provider


def _compile(expr):
    return str(expr.compile(compile_kwargs={'literal_binds': True}))


def _full_collection_row(**overrides):
    attrs = {
        'id': 'nlcd-LndCov',
        'type': 'Collection',
        'stac_version': '1.1.0',
        'title': 'NLCD Land Cover',
        'description': 'Annual NLCD land cover',
        'license': 'proprietary',
        'extent': {'spatial': {'bbox': [[-180, -90, 180, 90]]}},
        'links': [{'rel': 'self', 'href': 'https://example/c'}],
        'assets': {'thumbnail': {'href': 's3://x.png'}},
        'summaries': {'datetime': ['2006']},
        'stac_extensions': ['https://example/ext.json'],
        'sci:doi': '10.5066/example',
        'cube:dimensions': {'x': {}},
        'cube:variables': {'v': {}},
        'properties': {'extra': 'kept'},
        'geometry': {'type': 'Polygon', 'coordinates': []},
        'start_datetime': '2001-01-01T00:00:00Z',
        'end_datetime': '2021-12-31T00:00:00Z',
        'created_at': '2026-01-01T00:00:00Z',
        'updated_at': '2026-01-01T00:00:00Z',
    }
    attrs.update(overrides)
    return _Row(**attrs)


def _full_row(**overrides):
    attrs = {
        'id': 'Annual_NLCD_LndCov_2006',
        'collection': 'nlcd-LndCov',
        'type': 'Feature',
        'stac_version': '1.1.0',
        'geometry': {'type': 'Point', 'coordinates': [1.0, 2.0]},
        'bbox': [1.0, 2.0, 3.0, 4.0],
        'properties': {'datetime': '2006-01-01T00:00:00Z'},
        'assets': {'data': {'href': 's3://x.tif'}},
        'links': [{'rel': 'self', 'href': 'https://example/x'}],
        'created_at': '2026-01-01T00:00:00Z',
        'updated_at': '2026-01-01T00:00:00Z',
        'start_datetime': None,
        'end_datetime': None,
    }
    attrs.update(overrides)
    return _Row(**attrs)


# ---- reshape -------------------------------------------------------------

def test_reshape_lifts_stac_fields_to_top_level():
    feature = _make_provider()._sqlalchemy_to_feature(_full_row())

    assert feature['type'] == 'Feature'
    assert feature['id'] == 'Annual_NLCD_LndCov_2006'
    assert feature['collection'] == 'nlcd-LndCov'
    assert feature['stac_version'] == '1.1.0'
    assert feature['assets'] == {'data': {'href': 's3://x.tif'}}
    assert feature['links'] == [{'rel': 'self', 'href': 'https://example/x'}]
    assert feature['bbox'] == [1.0, 2.0, 3.0, 4.0]
    assert feature['geometry']['type'] == 'Point'


def test_reshape_properties_is_the_jsonb_blob_not_nested():
    feature = _make_provider()._sqlalchemy_to_feature(_full_row())

    # properties is the stored JSONB, not a bag with assets/links/properties
    assert feature['properties'] == {'datetime': '2006-01-01T00:00:00Z'}
    assert 'assets' not in feature['properties']
    assert 'links' not in feature['properties']
    assert 'properties' not in feature['properties']


def test_reshape_does_not_leak_administrative_columns():
    feature = _make_provider()._sqlalchemy_to_feature(_full_row())

    for key in ('created_at', 'updated_at', 'start_datetime', 'end_datetime'):
        assert key not in feature
        assert key not in feature['properties']


def test_reshape_derives_bbox_from_geometry_when_null():
    feature = _make_provider()._sqlalchemy_to_feature(_full_row(bbox=None))

    assert feature['bbox'] == [1.0, 2.0, 1.0, 2.0]


def test_reshape_null_geometry_yields_no_bbox():
    feature = _make_provider()._sqlalchemy_to_feature(
        _full_row(geometry=None, bbox=None))

    assert feature['geometry'] is None
    assert 'bbox' not in feature


def test_reshape_defaults_for_missing_jsonb_and_version():
    row = _Row(id='x', collection='c', geometry=None)
    feature = _make_provider()._sqlalchemy_to_feature(row)

    assert feature['properties'] == {}
    assert feature['assets'] == {}
    assert feature['links'] == []
    assert feature['stac_version'] == DEFAULT_STAC_VERSION


# ---- collection scoping --------------------------------------------------

def test_property_filters_unscoped_passthrough():
    # No collection configured -> behave like the parent (let everything
    # through when there are no property filters).
    assert _make_provider(collection=None)._get_property_filters([]) is True


def test_property_filters_scoped_with_no_properties():
    expr = _make_provider('nlcd-LndCov')._get_property_filters([])
    sql = _compile(expr)

    assert 'stac_items.collection' in sql
    assert 'nlcd-LndCov' in sql


def test_property_filters_scoped_ands_with_properties():
    provider = _make_provider('nlcd-LndCov')
    expr = provider._get_property_filters([('type', 'Feature')])
    sql = _compile(expr)

    assert 'stac_items.collection' in sql
    assert 'nlcd-LndCov' in sql
    assert 'stac_items.type' in sql
    assert 'Feature' in sql


# ---- mode switch ---------------------------------------------------------

def test_unknown_mode_rejected():
    with pytest.raises(ValueError):
        STACSQLProvider({'mode': 'nonsense'})


# ---- collections reshape -------------------------------------------------

def test_collection_reshape_lifts_stac_fields():
    coll = _make_collections_provider()._sqlalchemy_to_feature(
        _full_collection_row())

    assert coll['type'] == 'Collection'
    assert coll['id'] == 'nlcd-LndCov'
    assert coll['stac_version'] == '1.1.0'
    assert coll['title'] == 'NLCD Land Cover'
    assert coll['license'] == 'proprietary'
    assert coll['extent'] == {'spatial': {'bbox': [[-180, -90, 180, 90]]}}
    assert coll['assets'] == {'thumbnail': {'href': 's3://x.png'}}
    assert coll['links'] == [{'rel': 'self', 'href': 'https://example/c'}]


def test_collection_reshape_lifts_extension_and_properties_blobs():
    coll = _make_collections_provider()._sqlalchemy_to_feature(
        _full_collection_row())

    assert coll['sci:doi'] == '10.5066/example'
    assert coll['cube:dimensions'] == {'x': {}}
    assert coll['cube:variables'] == {'v': {}}
    # residual JSONB properties merge to top level
    assert coll['extra'] == 'kept'


def test_collection_reshape_drops_derived_columns():
    coll = _make_collections_provider()._sqlalchemy_to_feature(
        _full_collection_row())

    for key in ('geometry', 'bbox', 'start_datetime', 'end_datetime',
                'created_at', 'updated_at', 'properties'):
        assert key not in coll


def test_collection_reshape_defaults_for_missing_fields():
    row = _Row(id='c1')
    coll = _make_collections_provider()._sqlalchemy_to_feature(row)

    assert coll['type'] == 'Collection'
    assert coll['stac_version'] == DEFAULT_STAC_VERSION
    assert coll['description'] == ''
    assert coll['links'] == []
    assert 'title' not in coll


# ---- collections datetime overlap ----------------------------------------

def test_datetime_overlap_instant():
    expr = _make_collections_provider()._get_datetime_filter(
        '2010-01-01T00:00:00Z')
    sql = _compile(expr)

    assert 'start_datetime' in sql
    assert 'end_datetime' in sql
    assert 'IS NULL' in sql  # open (NULL) bounds treated as unbounded


def test_datetime_overlap_open_ended_range():
    # begin/.. -> only the lower-bound (end >= begin) clause is emitted
    expr = _make_collections_provider()._get_datetime_filter(
        '2010-01-01T00:00:00Z/..')
    sql = _compile(expr)

    assert 'end_datetime' in sql
    assert 'start_datetime' not in sql


def test_datetime_open_interval_is_passthrough():
    assert _make_collections_provider()._get_datetime_filter('../..') is True
    assert _make_collections_provider()._get_datetime_filter(None) is True


# ---- collections free-text (q) -------------------------------------------

def test_freetext_builds_title_description_ilike():
    provider = _make_collections_provider()
    expr = provider._get_property_filters([(provider._Q_SENTINEL, 'land')])
    sql = _compile(expr).lower()

    assert 'title' in sql
    assert 'description' in sql
    # .ilike() renders case-insensitively: the postgres ILIKE operator on a
    # live DB, lower(..) LIKE lower(..) under the default compile dialect.
    assert 'like' in sql
    assert 'lower' in sql
    assert 'land' in sql


def test_freetext_sentinel_not_treated_as_column_filter():
    # The sentinel is consumed, not passed to the parent equality filter.
    provider = _make_collections_provider()
    expr = provider._get_property_filters([(provider._Q_SENTINEL, 'x')])
    sql = _compile(expr).lower()

    assert provider._Q_SENTINEL.lower() not in sql
