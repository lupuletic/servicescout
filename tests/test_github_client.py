"""GitHub client: token validation, repo listing, pagination, error mapping."""

import io
import json
import unittest
import urllib.error
from unittest import mock

from servicescout import github_client as gh


class FakeResp:
    def __init__(self, body, headers=None):
        self._body = json.dumps(body).encode("utf-8")
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class ValidateTokenTests(unittest.TestCase):
    def test_parses_login_and_orgs(self) -> None:
        responses = [
            FakeResp({"login": "octocat"}),                       # /user
            FakeResp([{"login": "acme"}, {"login": "globex"}]),   # /user/orgs
        ]
        with mock.patch("urllib.request.urlopen", side_effect=responses):
            out = gh.validate_token("tok")
        self.assertEqual(out["login"], "octocat")
        self.assertEqual([o["login"] for o in out["orgs"]], ["acme", "globex"])

    def test_401_maps_to_githuberror(self) -> None:
        err = urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b""))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(gh.GithubError) as ctx:
                gh.validate_token("bad")
        self.assertEqual(ctx.exception.status, 401)


class ListOrgReposTests(unittest.TestCase):
    def test_filters_archived_and_maps_fields(self) -> None:
        page = FakeResp([
            {"name": "web", "full_name": "acme/web", "description": "store", "language": "Go", "archived": False},
            {"name": "old", "full_name": "acme/old", "archived": True},
        ])
        with mock.patch("urllib.request.urlopen", return_value=page):
            repos = gh.list_org_repos("tok", "acme")
        self.assertEqual(len(repos), 1)
        self.assertEqual(repos[0], {"name": "web", "full_name": "acme/web", "description": "store", "language": "Go"})

    def test_follows_link_header_pagination(self) -> None:
        p1 = FakeResp(
            [{"name": "a", "full_name": "acme/a"}],
            headers={"Link": '<https://api.github.com/orgs/acme/repos?page=2>; rel="next"'},
        )
        p2 = FakeResp([{"name": "b", "full_name": "acme/b"}], headers={})
        with mock.patch("urllib.request.urlopen", side_effect=[p1, p2]):
            repos = gh.list_org_repos("tok", "acme")
        self.assertEqual([r["full_name"] for r in repos], ["acme/a", "acme/b"])


if __name__ == "__main__":
    unittest.main()
