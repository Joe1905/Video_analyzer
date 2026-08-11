"""Focused checks for the locally maintained SociaVault credit balance."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import sociavault_usage


class SociaVaultCreditBalanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.original_usage_file = sociavault_usage.USAGE_FILE
        self.original_balance_file = sociavault_usage.BALANCE_FILE
        sociavault_usage.USAGE_FILE = root / "usage.json"
        sociavault_usage.BALANCE_FILE = root / "balance.json"

    def tearDown(self) -> None:
        sociavault_usage.USAGE_FILE = self.original_usage_file
        sociavault_usage.BALANCE_FILE = self.original_balance_file
        self.temporary.cleanup()

    def test_authoritative_snapshot_is_decremented_by_explicit_usage(self) -> None:
        snapshot = sociavault_usage.set_sociavault_credit_balance(100, "active")
        self.assertEqual(snapshot["credits"], 100)
        self.assertFalse(snapshot["estimated"])

        maintained = sociavault_usage.record_sociavault_credits_used(7, source="sociavault__search")
        self.assertEqual(maintained["credits"], 93)
        self.assertEqual(maintained["last_credits_used"], 7)
        self.assertTrue(maintained["estimated"])

    def test_cache_hit_does_not_consume_local_balance(self) -> None:
        sociavault_usage.set_sociavault_credit_balance(50, "active")
        maintained = sociavault_usage.record_sociavault_credits_used(5, cache_hit=True)
        self.assertEqual(maintained["credits"], 50)
        self.assertFalse(maintained["estimated"])

    def test_rest_response_credits_used_updates_existing_snapshot(self) -> None:
        sociavault_usage.set_sociavault_credit_balance(20, "active")
        response = SimpleNamespace(status_code=200, headers={})
        sociavault_usage.update_sociavault_usage_from_response(response, {"credits_used": 2})
        self.assertEqual(sociavault_usage.read_sociavault_credit_balance()["credits"], 18)

    def test_usage_without_snapshot_does_not_invent_balance(self) -> None:
        maintained = sociavault_usage.record_sociavault_credits_used(3)
        self.assertIsNone(maintained["credits"])
        self.assertFalse(maintained["observed"])
        self.assertFalse(sociavault_usage.BALANCE_FILE.exists())

    def test_balance_file_and_public_read_are_sanitized(self) -> None:
        sociavault_usage.set_sociavault_credit_balance(16030, "active")
        public = sociavault_usage.read_sociavault_credit_balance()
        stored = json.loads(sociavault_usage.BALANCE_FILE.read_text(encoding="utf-8"))
        self.assertNotIn("subscriptionId", public)
        self.assertNotIn("subscription_id", public)
        self.assertNotIn("subscriptionId", stored)
        self.assertEqual(public["subscription_status"], "active")

    def test_nested_mcp_charge_is_detected(self) -> None:
        payload = {"structuredContent": {"data": {"items": []}, "creditsUsed": 1}}
        self.assertEqual(sociavault_usage.extract_credits_used(payload), 1)


if __name__ == "__main__":
    unittest.main()
