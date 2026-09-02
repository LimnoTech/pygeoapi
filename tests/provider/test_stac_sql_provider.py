# =================================================================
#
# Authors: ttuhinanshu <ttuhinanshu@element84.com>
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

# These are unit tests for the two seams STACItemsProvider adds on top of
# PostgreSQLProvider -- the row->STAC-Item reshape and the collection scope.
# They do not require a live database: the provider is instantiated without
# connecting (``__new__``) and the reshape/filter methods are exercised
# directly. End-to-end coverage against a real ``stac_items`` table belongs in
# an integration test alongside test_postgresql_provider.py.

from sqlalchemy import Column, String
from sqlalchemy.orm import declarative_base

from pygeoapi.provider.stac_sql import STACItemsProvider, DEFAULT_STAC_VERSION


_Base = declarative_base()


class _FakeItems(_Base):
    """Minimal stand-in for the reflected stac_items model."""
    __tablename__ = 'stac_items'
    id = Column(String, primary_key=True)
    collection = Column(String)
    type = Column(String)


class _Row:
    """Stand-in for a reflected SQLAlchemy row (attributes via __dict__)."""
    def __init__(self, **attrs):
        self.__dict__.update(attrs)


def _make_provider(collection=None):
    """Build a provider without running __init__ (no DB connection)."""
    provider = STACItemsProvider.__new__(STACItemsProvider)
    provider.id_field = 'id'
    provider.geom = 'geometry'
    provider.collection_field = 'collection'
    provider.collection = collection
    # get_table_model() is annotated `-> Table` but actually returns an
    # automapped ORM class (see sql.py); _FakeItems matches that runtime
    # shape, so silence the static-only mismatch against the Table annotation.
    provider.table_model = _FakeItems  # type: ignore[assignment]
    return provider


def _compile(expr):
    return str(expr.compile(compile_kwargs={'literal_binds': True}))


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
