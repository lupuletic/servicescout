"""Tests for the optional MCP auth layer (Epic #9 Tier 4 #9).

These exercise the configuration + filtering logic without bringing up
a real OIDC issuer. Token validation against real JWKS is covered by
manual smoke testing — the tests here verify the contract / glue.
"""

import unittest
from unittest import mock

import auth


class AuthConfigTests(unittest.TestCase):
    def test_default_is_disabled(self) -> None:
        cfg = auth.AuthConfig()
        self.assertFalse(cfg.enabled)

    def test_from_env_disabled_without_issuer(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            cfg = auth.AuthConfig.from_env()
            self.assertFalse(cfg.enabled)

    def test_from_env_enabled_with_issuer(self) -> None:
        env = {"MCP_AUTH_ISSUER": "https://idp.example.com",
               "MCP_AUTH_AUDIENCE": "servicescout",
               "MCP_AUTH_PUBLIC_OWNERS": "*"}
        with mock.patch.dict("os.environ", env, clear=True):
            cfg = auth.AuthConfig.from_env()
            self.assertTrue(cfg.enabled)
            self.assertEqual(cfg.issuer, "https://idp.example.com")
            self.assertEqual(cfg.audience, "servicescout")


class ValidateDisabledTests(unittest.TestCase):
    def test_disabled_auth_grants_unrestricted_caller(self) -> None:
        caller = auth.validate("", auth.AuthConfig(enabled=False))
        self.assertTrue(caller.is_unrestricted)
        self.assertEqual(caller.sub, "anonymous")

    def test_disabled_auth_ignores_token(self) -> None:
        caller = auth.validate("any-garbage", auth.AuthConfig(enabled=False))
        self.assertTrue(caller.is_unrestricted)


class ValidateEnabledTests(unittest.TestCase):
    def test_missing_token_raises(self) -> None:
        cfg = auth.AuthConfig(enabled=True, issuer="https://idp.example.com")
        with self.assertRaises(auth.AuthError):
            auth.validate("", cfg)

    def test_unknown_mode_raises(self) -> None:
        cfg = auth.AuthConfig(enabled=True, issuer="x", mode="bogus")
        with self.assertRaises(auth.AuthError):
            auth.validate("token", cfg)


class FilterEntityTests(unittest.TestCase):
    def _entity(self, owner: str | None = None, annotations_owner: str | None = None) -> dict:
        return {
            "kind": "Component",
            "metadata": {"name": "x", "annotations": {"owner": annotations_owner or ""}},
            "spec": {"owner": owner or ""},
        }

    def test_unrestricted_caller_sees_everything(self) -> None:
        caller = auth.Caller(sub="x", teams={"*"}, raw_claims={})
        self.assertTrue(auth.filter_entity(self._entity(owner="team-orders"), caller))

    def test_owner_match_allows(self) -> None:
        caller = auth.Caller(sub="x", teams={"team-orders"}, raw_claims={})
        self.assertTrue(auth.filter_entity(self._entity(owner="team-orders"), caller))

    def test_owner_mismatch_denies(self) -> None:
        caller = auth.Caller(sub="x", teams={"team-orders"}, raw_claims={})
        self.assertFalse(auth.filter_entity(self._entity(owner="team-shipping"), caller))

    def test_no_owner_allows(self) -> None:
        caller = auth.Caller(sub="x", teams={"team-orders"}, raw_claims={})
        self.assertTrue(auth.filter_entity(self._entity(owner=""), caller))

    def test_unknown_owner_treated_as_no_owner(self) -> None:
        caller = auth.Caller(sub="x", teams={"team-orders"}, raw_claims={})
        self.assertTrue(auth.filter_entity(self._entity(owner="unknown"), caller))

    def test_annotations_owner_takes_priority_when_spec_empty(self) -> None:
        caller = auth.Caller(sub="x", teams={"team-orders"}, raw_claims={})
        self.assertTrue(auth.filter_entity(
            self._entity(owner="", annotations_owner="team-orders"), caller
        ))

    def test_backstage_owner_namespacing_is_stripped(self) -> None:
        # Backstage stores `group:default/team-orders`. The caller's
        # teams claim might just say `team-orders`.
        caller = auth.Caller(sub="x", teams={"team-orders"}, raw_claims={})
        self.assertTrue(auth.filter_entity(
            self._entity(owner="group:default/team-orders"), caller
        ))


class ClaimsToCallerTests(unittest.TestCase):
    def test_extracts_teams_from_list_claim(self) -> None:
        cfg = auth.AuthConfig(enabled=True, teams_claim="groups")
        caller = auth._claims_to_caller(
            {"sub": "user-1", "groups": ["team-orders", "team-shipping"]}, cfg
        )
        self.assertEqual(caller.sub, "user-1")
        self.assertIn("team-orders", caller.teams)
        self.assertIn("team-shipping", caller.teams)

    def test_extracts_teams_from_space_separated_string(self) -> None:
        # Disable the wildcard public-owners side effect so we test the
        # team-extraction logic in isolation.
        cfg = auth.AuthConfig(enabled=True, teams_claim="scope", public_owners=[])
        caller = auth._claims_to_caller(
            {"sub": "user-1", "scope": "team-orders team-payments"}, cfg
        )
        self.assertEqual(caller.teams, {"team-orders", "team-payments"})

    def test_wildcard_public_owners_grants_unrestricted(self) -> None:
        cfg = auth.AuthConfig(enabled=True, public_owners=["*"])
        caller = auth._claims_to_caller(
            {"sub": "user-1", "groups": ["random-team"]}, cfg
        )
        self.assertTrue(caller.is_unrestricted)


if __name__ == "__main__":
    unittest.main()
