"""Offline transport boundary, not a replacement Add host or save implementation.

Only exact provider requests get fixture HTTP responses. Production provider parsing,
authentication, CSRF, actor capture, signing, templates, native operations and SQLite
remain in use. Importing this module does not import app.main or read signing keys.
The three real cassette projections are attributed in the adjacent NEW JSON fixture;
all other responses and identities are explicitly synthetic. Artwork is NEVER served.
"""

import copy
import json
import re
from pathlib import Path
from urllib.parse import urlsplit


TMDB_CREDENTIAL = "add-media-fixture-tmdb"
IGDB_CLIENT = "add-media-fixture-client"
IGDB_SECRET = "add-media-fixture-secret"
IGDB_ACCESS = "add-media-fixture-access"
DISCOGS_CREDENTIAL = "add-media-fixture-discogs"


def load_catalogue():
    return json.loads((Path(__file__).resolve().parents[1] / "fixtures" /
                       "add_media_catalogue.json").read_text(encoding="utf-8"))


def _mb_query(*, title="", artist="", catno="", barcode="", kind="cassette", country=""):
    def literal(value):
        return re.sub(r'([+\-!(){}\[\]^"~*?:\\/|&])', r'\\\1', value)

    clauses = [f'{key}:"{literal(value)}"' for key, value in (
        ("barcode", barcode), ("catno", catno), ("release", title), ("artist", artist)) if value]
    formats = {"cassette": "Cassette", "cd": "CD", "vinyl": "(Vinyl OR 7vinyl OR 10vinyl OR 12vinyl)",
               "digital_music": "DigitalMedia"}
    clauses.append("format:" + formats[kind])
    if country:
        clauses.append(f'country:"{country}"')
    return " AND ".join(clauses)


def _discogs_params(*, title="", artist="", catno="", barcode="", kind="cassette", country=""):
    result = {"type": "release", "per_page": "20", "page": "1",
              "format": {"cassette": "Cassette", "cd": "CD", "vinyl": "Vinyl", "digital_music": "File"}[kind]}
    result.update({key: value for key, value in (("release_title", title), ("artist", artist),
                  ("catno", catno), ("barcode", barcode), ("country", country)) if value})
    return result


def _search_row(release):
    """Synthetic search projection; not an assertion of live search/ranking."""
    artist = ", ".join(value["name"] for value in release["artists"])
    return {"id": release["id"], "type": "release", "title": artist + " - " + release["title"],
            "artists": release["artists"], "year": release["year"], "country": release["country"],
            "label": [label["name"] for label in release["labels"]],
            "catno": release["labels"][0]["catno"], "format": [f["name"] for f in release["formats"]],
            "uri": f'/release/{release["id"]}'}


class FixtureCatalogue:
    """Closed outbound allowlist with a durable unexpected-request ledger.

    Clients catch exceptions. Raising alone would therefore be a false negative;
    every rejected call is also audited and the browser fixture asserts it clean.
    Header/token values are never written to that ledger.
    """

    SCENARIOS = {"normal", "discogs_rejected", "discogs_rate_limited", "discogs_transport",
                 "discogs_unreadable", "mb_transport", "format_conflict", "detail_mismatch",
                 "detail_unreadable", "no_credentials", "music_disabled"}

    def __init__(self, *, scenario="normal", audit_path=None, upc_url=None):
        if scenario not in self.SCENARIOS:
            raise ValueError("Unknown Add Media fixture scenario")
        self.scenario, self.audit_path, self.upc_url = scenario, audit_path, upc_url
        self.calls, self.unexpected = [], []
        self.catalogue = load_catalogue()
        self.mb_searches, self.mb_details, self.discogs_searches = {}, {}, {}
        self.discogs_details = copy.deepcopy(self.catalogue["discogs_releases"])
        self._prepare()

    def _prepare(self):
        empty = {"created": "2026-11-16T00:00:00Z", "count": 0, "offset": 0, "releases": []}
        known = {"pagination": {"page": 1, "pages": 1}, "results": [
            _search_row(value) for value in self.discogs_details.values()]}

        def discogs(parameters, payload):
            self.discogs_searches[tuple(sorted(parameters.items()))] = payload

        for country in ("", "GB"):
            self.mb_searches[_mb_query(title="Hits Out of Hell", artist="Meat Loaf", country=country)] = empty
        for catno in ("40-26156", "4026156"):
            self.mb_searches[_mb_query(catno=catno)] = empty
        discogs(_discogs_params(title="Hits Out of Hell", artist="Meat Loaf"), known)
        discogs(_discogs_params(title="Hits Out of Hell", artist="Meat Loaf", catno="40-26156"), known)
        discogs(_discogs_params(catno="40-26156"), known)
        discogs(_discogs_params(catno="4026156"), {"results": []})
        discogs(_discogs_params(title="No identified pressing", artist="Fixture Ensemble"), {"results": []})
        discogs(_discogs_params(title="Provider Failure Probe", artist="Fixture Ensemble"), {"results": []})

        for index, (kind, format_name) in enumerate((("cassette", "Cassette"), ("cd", "CD"),
                                                    ("vinyl", "Vinyl"), ("digital_music", "Digital Media")), 1):
            release = copy.deepcopy(self.catalogue["musicbrainz_release"])
            release["id"] = f"11111111-1111-4111-8111-{111111111100 + index:012d}"
            release["media"][0]["format"] = format_name
            release["label-info"][0]["catalog-number"] = f"SYN-MB-{kind.upper()}-01"
            self.mb_details[release["id"]] = release
            for country in ("", "IT"):
                self.mb_searches[_mb_query(title="Paper Lantern Sessions", artist="Fixture Ensemble",
                                          kind=kind, country=country)] = {"releases": [release]}
            synthetic = {
                "id": 910000 + index, "title": "Synthetic Format Probe", "country": "Italy", "year": 2002,
                "released": "2002", "artists": [{"name": "Fixture Ensemble"}],
                "labels": [{"name": "Fixture Label", "catno": f"SYN-D-{kind.upper()}"}],
                "formats": [{"name": "File" if kind == "digital_music" else format_name, "qty": "1"}],
                "tracklist": [{"position": "1", "title": "Synthetic reference track", "type_": "track", "duration": "1:01"}],
            }
            self.discogs_details[str(synthetic["id"])] = synthetic
            discogs(_discogs_params(title="Synthetic Format Probe", artist="Fixture Ensemble", kind=kind),
                    {"results": [_search_row(synthetic)]})
        mixed = copy.deepcopy(self.mb_details["11111111-1111-4111-8111-111111111102"])
        mixed.update(id="11111111-1111-4111-8111-111111111105", title="Mixed Medium Probe")
        second = copy.deepcopy(mixed["media"][0])
        second.update(position=2, format="Vinyl")
        mixed["media"].append(second)
        self.mb_details[mixed["id"]] = mixed
        self.mb_searches[_mb_query(title="Mixed Medium Probe", artist="Fixture Ensemble", kind="cd")] = {"releases": [mixed]}
        hostile = copy.deepcopy(self.discogs_details["910001"])
        hostile.update(id=910099, title='<img src=x onerror="alert(1)"> & <script>window.BAD=1</script> ' + "LongMetadata" * 12)
        hostile["artists"] = [{"name": '<svg onload="alert(2)">Fixture & Ensemble'}]
        hostile["labels"] = [{"name": '<b onclick="alert(3)">Label & Co</b>', "catno": "LONG-" + "X" * 90}]
        hostile["tracklist"][0]["title"] = '<iframe src="https://evil.invalid/">not markup</iframe>'
        self.discogs_details["910099"] = hostile
        discogs(_discogs_params(title="Hostile Metadata", artist="Fixture Ensemble"), {"results": [_search_row(hostile)]})

    def assert_clean(self):
        assert not self.unexpected, f"Unexpected provider requests: {self.unexpected!r}"

    def _record(self, record):
        self.calls.append(record)
        if self.audit_path:
            with Path(self.audit_path).open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, sort_keys=True) + "\n")

    async def fetch(self, client, method, url, **kwargs):
        import httpx

        # Duplicate query parameters are not silently flattened into an allowlist.
        raw_params = kwargs.get("params") or {}
        normalization_failed = False
        try:
            if hasattr(raw_params, "multi_items"):
                pairs = list(raw_params.multi_items())
            elif hasattr(raw_params, "items"):
                pairs = list(raw_params.items())
            else:
                pairs = list(raw_params)
            parameters = {str(key): str(value) for key, value in pairs}
        except (TypeError, ValueError):
            parameters, pairs, normalization_failed = {}, [], True
        try:
            headers = httpx.Headers(kwargs.get("headers") or {})
        except (TypeError, ValueError):
            headers, normalization_failed = httpx.Headers(), True
        try:
            parts = urlsplit(str(url))
            port = parts.port  # Invalid ports must also be retained, not just raised.
            authority = str(parts.hostname) + (f":{port}" if port is not None else "")
            safe_url = f"{parts.scheme}://{authority}{parts.path}"
        except ValueError:
            parts, safe_url, normalization_failed = None, "<malformed-url>", True
        data = kwargs.get("data") or {}
        data_keys = sorted(str(key) for key in data) if hasattr(data, "keys") else ["<non-mapping>"]
        content = kwargs.get("content")
        record = {"method": method, "url": safe_url,
                  "params": {key: ("<redacted>" if key in {"api_key", "token", "client_secret"} else value)
                             for key, value in parameters.items()},
                  "authorization_present": "authorization" in headers,
                  "data_keys": data_keys,
                  "content": (content if isinstance(content, str) else "<non-text>")
                             if parts is not None and parts.hostname == "api.igdb.com" else None}
        spec = None
        safe = (not normalization_failed and parts is not None
                and not parts.username and not parts.password and not parts.query and not parts.fragment
                and len(pairs) == len(parameters)
                and kwargs.get("json") is None
                and (method != "GET" or not (kwargs.get("data") or kwargs.get("content")))
                and ((parts.scheme == "https" and port in (None, 443)) or str(url) == self.upc_url))
        if safe:
            spec = self._match(method, str(url), parameters, headers, kwargs)
        if spec is None:
            record["unexpected"] = True
            self.unexpected.append(record)
            self._record(record)
            raise AssertionError(f"Unauthorized fixture outbound request: {record!r}")
        self._record(record)
        request = httpx.Request(method, str(url), params=parameters)
        if spec.get("transport_error"):
            raise httpx.ConnectError("Synthetic provider transport failure", request=request)
        options = {"text": spec["text"]} if "text" in spec else {"json": spec["json"]}
        return httpx.Response(spec.get("status", 200), headers=spec.get("headers"), request=request, **options)

    def _match(self, method, url, params, headers, kwargs):
        def response(payload):
            return {"json": payload}

        if method == "GET" and url == "https://musicbrainz.org/ws/2/release/":
            if (set(params) == {"query", "fmt", "limit"} and params["fmt"] == "json"
                    and params["limit"] == "30" and params["query"] in self.mb_searches
                    and "authorization" not in headers):
                if self.scenario == "mb_transport":
                    return {"transport_error": True}
                return response(self.mb_searches[params["query"]])
        if method == "GET" and url.startswith("https://musicbrainz.org/ws/2/release/"):
            identifier = url.removeprefix("https://musicbrainz.org/ws/2/release/")
            if (identifier in self.mb_details and params == {"fmt": "json", "inc": "artist-credits+labels+recordings+release-groups"}
                    and "authorization" not in headers):
                return response(self.mb_details[identifier])
        if method == "GET" and url == "https://api.discogs.com/database/search":
            key = tuple(sorted(params.items()))
            if (key in self.discogs_searches and headers.get("authorization", "") in
                    ("", f"Discogs token={DISCOGS_CREDENTIAL}")):
                failures = {"discogs_rejected": {"status": 401, "json": {"message": "SYNTHETIC-DO-NOT-REFLECT"}},
                            "discogs_rate_limited": {"status": 429, "headers": {"Retry-After": "60"}, "json": {}},
                            "discogs_transport": {"transport_error": True},
                            "discogs_unreadable": {"text": "SYNTHETIC-DO-NOT-REFLECT <broken>"}}
                return failures.get(self.scenario, response(self.discogs_searches[key]))
        if method == "GET" and url.startswith("https://api.discogs.com/releases/"):
            identifier = url.removeprefix("https://api.discogs.com/releases/")
            if (identifier in self.discogs_details and not params and headers.get("authorization", "") in
                    ("", f"Discogs token={DISCOGS_CREDENTIAL}")):
                release = copy.deepcopy(self.discogs_details[identifier])
                if self.scenario == "format_conflict":
                    release["formats"] = [{"name": "CD", "qty": "1"}]
                elif self.scenario == "detail_mismatch":
                    release["id"] = 910098
                elif self.scenario == "detail_unreadable":
                    return {"text": "SYNTHETIC-DO-NOT-REFLECT"}
                return response(release)
        if method == "GET" and "authorization" not in headers:
            search_fields = ("key,title,author_name,first_publish_year,publisher,cover_i,isbn,"
                             "number_of_pages_median,language,editions,editions.isbn")
            if url == "https://openlibrary.org/search.json" and params == {
                    "q": "The Synthetic Atlas", "limit": "10", "fields": search_fields, "lang": "en"}:
                return response(self.catalogue["openlibrary_search"])
            resources = {"https://openlibrary.org/isbn/9780306406157.json": "openlibrary_edition",
                         "https://openlibrary.org/works/OL9990001W.json": "openlibrary_work",
                         "https://openlibrary.org/authors/OL9990001A.json": "openlibrary_author"}
            if url in resources and not params:
                return response(self.catalogue[resources[url]])
            if url == self.upc_url and params == {"upc": "012345678905"}:
                return response(self.catalogue["upc_lookup"])
        if (method == "GET" and url == "https://api.themoviedb.org/3/search/movie"
                and params == {"query": "Synthetic Cinema"}
                and headers.get("authorization") == f"Bearer {TMDB_CREDENTIAL}"):
            return response(self.catalogue["tmdb_search"])
        if (method == "POST" and url == "https://id.twitch.tv/oauth2/token" and not params
                and kwargs.get("data") == {"client_id": IGDB_CLIENT, "client_secret": IGDB_SECRET,
                                           "grant_type": "client_credentials"}):
            return response({"access_token": IGDB_ACCESS, "expires_in": 3600, "token_type": "bearer"})
        if (method == "POST" and url == "https://api.igdb.com/v4/games" and not params
                and headers.get("authorization") == f"Bearer {IGDB_ACCESS}"
                and headers.get("client-id") == IGDB_CLIENT):
            fields = ("fields name, platforms.name, first_release_date, involved_companies.company.name, "
                      "involved_companies.publisher, cover.image_id, summary, franchises.name")
            searches = {f'search "Synthetic Switch Quest"; {fields}; {where}limit 10;'
                        for where in ("", "where platforms = (130); ")}
            detail = ("fields name, platforms.name, first_release_date, involved_companies.company.name, "
                      "involved_companies.publisher, involved_companies.developer, "
                      "cover.image_id, summary, franchises.name; where id = 920001;")
            if kwargs.get("content") in searches | {detail}:
                return response(self.catalogue["igdb_games"])
        return None


def create_app():
    """uvicorn tests.e2e.add_media_asgi:create_app --factory (no test routes)."""
    import os

    import httpx

    from app.services import outbound

    fixture = FixtureCatalogue(scenario=os.environ.get("SHELF_ADD_MEDIA_SCENARIO", "normal"),
                               audit_path=os.environ.get("SHELF_ADD_MEDIA_CALLS"),
                               upc_url=os.environ.get("SHELF_UPC_LOOKUP_URL"))
    outbound.fetch = fixture.fetch

    # Legacy Open Library uses AsyncClient.get/request instead of outbound.fetch.
    # Bridge that HTTP entry to the SAME exact transport allowlist, never patch its
    # parsers/lookup functions and never let an unrecognized direct call go live.
    async def direct_request(client, method, url, **kwargs):
        return await fixture.fetch(client, method, url, **kwargs)

    httpx.AsyncClient.request = direct_request
    # Deliberately after fresh DATA_DIR/environment and fixture transport setup.
    from app.main import app

    app.state.add_media_catalogue = fixture
    if os.environ.get("SHELF_ADD_MEDIA_PREFIX") == "/shelf":
        from contextlib import asynccontextmanager

        from starlette.applications import Starlette
        from starlette.routing import Mount

        @asynccontextmanager
        async def lifespan(mounted):
            # Mount does not run a child application's startup automatically.
            async with app.router.lifespan_context(app):
                yield

        # Both mounts are the SAME real production app. Existing setup/login keep
        # their native root redirects; only the tested Add journey uses /shelf.
        # This tests Add's url_for boundary without claiming unrelated legacy
        # setup/auth pages have acquired whole-application mounted readiness.
        return Starlette(routes=[Mount("/shelf", app=app), Mount("/", app=app)], lifespan=lifespan)
    return app
