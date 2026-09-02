# =================================================================
#
# Authors: Tom Kralidis <tomkralidis@gmail.com>
#
# Copyright (c) 2026 Tom Kralidis
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

import json

import pytest

from pygeoapi.api.stac import search, landing_page, _rewrite_item_links
from pygeoapi.formats import FORMAT_TYPES, F_JSON
from pygeoapi.util import yaml_load

from tests.util import get_test_file_path, mock_api_request


@pytest.fixture()
def config():
    with open(get_test_file_path('pygeoapi-test-stac-api-config.yml')) as fh:
        return yaml_load(fh)


def test_landing_page(config, api_):
    req = mock_api_request()
    rsp_headers, code, response = landing_page(api_, req)
    response = json.loads(response)

    assert rsp_headers['Content-Type'] == 'application/json' == \
           FORMAT_TYPES[F_JSON]

    assert isinstance(response, dict)
    assert 'links' in response
    assert len(response['conformsTo']) == 3
    assert response['type'] == 'Catalog'
    assert response['links'][0]['rel'] == 'self'
    assert response['links'][0]['type'] == 'application/json'
    assert response['links'][0]['href'] == 'http://localhost:5000/stac-api?f=json'  # noqa
    assert len(response['links']) == 5
    assert 'title' in response
    assert response['title'] == 'pygeoapi default instance'
    assert 'description' in response
    assert response['description'] == 'pygeoapi provides an API to geospatial data'  # noqa


@pytest.mark.parametrize('params,matched,returned', [
    ({}, 10, 10),
    ({'bbox': '-142,52,-140,55'}, 6, 6),
    ({'limit': '1'}, 10, 1),
    ({'datetime': '2019-11-11T11:11:11Z/..'}, 6, 6),
    ({'datetime': '2018-11-11T11:11:11Z/2019-11-11T11:11:11Z'}, 4, 4)
])
def test_search(config, api_, params, matched, returned):
    # test GET
    req = mock_api_request(params)
    rsp_headers, code, response = search(api_, req)
    response = json.loads(response)

    assert response['numberMatched'] == matched
    assert response['numberReturned'] == returned

    for feature in response['features']:
        assert feature['stac_version'] == '1.0.0'

    # test POST
    req = mock_api_request(data=params)
    rsp_headers, code, response = search(api_, req)
    response = json.loads(response)

    assert response['numberMatched'] == matched
    assert response['numberReturned'] == returned

    for feature in response['features']:
        assert feature['stac_version'] == '1.0.0'


def test_rewrite_item_links_mints_absolute_nav_links():
    feature = {
        'id': 'Annual_NLCD_LndCov_2006',
        'collection': 'nlcd-LndCov',
        'links': [
            {'rel': 'cite-as', 'href': 'https://doi.org/10.5066/P94UXNTS'},
            {'rel': 'root', 'href': '../../../catalog.json',
             'type': 'application/json'},
            {'rel': 'collection', 'href': '../collection.json',
             'type': 'application/json'},
            {'rel': 'parent', 'href': '../collection.json',
             'type': 'application/json'},
        ],
    }

    links = _rewrite_item_links('http://localhost:5000', feature)

    by_rel = {}
    for link in links:
        by_rel.setdefault(link['rel'], []).append(link['href'])

    stac = 'http://localhost:5000/stac-api'
    # relative nav links are replaced with absolute STAC API links
    assert by_rel['self'] == [
        f'{stac}/collections/nlcd-LndCov/items/Annual_NLCD_LndCov_2006?f=json']
    assert by_rel['root'] == [f'{stac}?f=json']
    assert by_rel['collection'] == [f'{stac}/collections/nlcd-LndCov?f=json']
    assert by_rel['parent'] == [f'{stac}/collections/nlcd-LndCov?f=json']

    # no relative hrefs remain (this is what broke pystac_client)
    all_hrefs = [h for hrefs in by_rel.values() for h in hrefs]
    assert all(h.startswith('http') for h in all_hrefs)

    # portable absolute links are preserved
    assert by_rel['cite-as'] == ['https://doi.org/10.5066/P94UXNTS']


def test_rewrite_item_links_without_collection_only_root():
    links = _rewrite_item_links('http://localhost:5000', {'id': 'x'})

    assert [link['rel'] for link in links] == ['root']
    assert links[0]['href'] == 'http://localhost:5000/stac-api?f=json'


def test_sortby_to_token_converts_stac_post_form():
    from pygeoapi.api.stac import _sortby_to_token

    # STAC POST elements: {field, direction} -> pygeoapi +/-field tokens
    assert _sortby_to_token({"field": "datetime", "direction": "asc"}) == "datetime"  # noqa: E501
    assert _sortby_to_token({"field": "datetime", "direction": "desc"}) == "-datetime"  # noqa: E501
    # missing direction defaults to ascending
    assert _sortby_to_token({"field": "id"}) == "id"
    # plain strings pass through unchanged
    assert _sortby_to_token("-datetime") == "-datetime"


def test_cql_in_builds_predicates():
    from pygeoapi.api.stac import _cql_in

    assert _cql_in("id", ["a"]) == "id = 'a'"
    assert _cql_in("id", ["a", "b"]) == "id IN ('a','b')"
    # single quotes in values are escaped (doubled)
    assert _cql_in("id", ["O'Brien"]) == "id = 'O''Brien'"
