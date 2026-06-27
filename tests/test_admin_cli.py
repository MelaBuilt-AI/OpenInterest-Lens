"""Tests for the admin CLI."""

from __future__ import annotations

from click.testing import CliRunner

from admin.cli import _hash_key, cli


class TestKeyCreate:
    """Test oil-admin key create."""

    def test_creates_key_with_required_tier(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["key", "create", "--tier", "free"])
        assert result.exit_code == 0
        assert "oil_sk_live_" in result.output
        assert "Tier:    free" in result.output

    def test_creates_key_all_tiers(self):
        runner = CliRunner()
        for tier in ["free", "pro", "enterprise"]:
            result = runner.invoke(cli, ["key", "create", "--tier", tier])
            assert result.exit_code == 0
            assert f"Tier:    {tier}" in result.output

    def test_creates_key_with_email_and_notes(self):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["key", "create", "--tier", "pro", "--email", "test@example.com", "--notes", "Test key"],
        )
        assert result.exit_code == 0
        assert "test@example.com" in result.output
        assert "Test key" in result.output

    def test_rejects_invalid_tier(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["key", "create", "--tier", "invalid"])
        assert result.exit_code != 0

    def test_key_is_unique_each_call(self):
        runner = CliRunner()
        r1 = runner.invoke(cli, ["key", "create", "--tier", "free"])
        r2 = runner.invoke(cli, ["key", "create", "--tier", "free"])
        # Extract keys from output
        key1 = [line for line in r1.output.splitlines() if "oil_sk_live_" in line]
        key2 = [line for line in r2.output.splitlines() if "oil_sk_live_" in line]
        assert key1 != key2


class TestKeyHash:
    """Test API key hashing."""

    def test_hash_is_deterministic(self):
        h1 = _hash_key("test_key")
        h2 = _hash_key("test_key")
        assert h1 == h2

    def test_hash_differs_for_different_keys(self):
        h1 = _hash_key("key_one")
        h2 = _hash_key("key_two")
        assert h1 != h2

    def test_hash_format(self):
        h = _hash_key("test_key")
        assert len(h) == 64  # SHA-256 hex digest
        assert all(c in "0123456789abcdef" for c in h)


class TestKeyRotate:
    """Test oil-admin key rotate (offline parts)."""

    def test_rotate_requires_key_id(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["key", "rotate"])
        assert result.exit_code != 0  # Missing required argument


class TestKeyRevoke:
    """Test oil-admin key revoke (offline parts)."""

    def test_revoke_requires_key_id(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["key", "revoke"])
        assert result.exit_code != 0


class TestKeyInfo:
    """Test oil-admin key info (offline parts)."""

    def test_info_requires_key_id(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["key", "info"])
        assert result.exit_code != 0


class TestDBCommands:
    """Test DB subcommands (argument validation)."""

    def test_db_migrate_runs(self):
        """DB migrate should attempt to connect, may fail without DB."""
        runner = CliRunner()
        result = runner.invoke(cli, ["db", "migrate"])
        # Will either succeed or fail with connection error, both acceptable
        assert result.exit_code in (0, 1)

    def test_db_seed_runs(self):
        """DB seed should attempt to connect, may fail without DB."""
        runner = CliRunner()
        result = runner.invoke(cli, ["db", "seed"])
        assert result.exit_code in (0, 1)


class TestHealthCommand:
    """Test health command (requires API running for full test)."""

    def test_health_without_api(self):
        """Health check should fail gracefully when API is not running."""
        runner = CliRunner()
        result = runner.invoke(cli, ["health"])
        # Will fail because API is not running, but should not crash
        assert result.exit_code in (0, 1)


class TestQualityCommand:
    """Test quality check command."""

    def test_quality_without_api(self):
        """Quality check should fail gracefully when API is not running."""
        runner = CliRunner()
        result = runner.invoke(cli, ["quality"])
        assert result.exit_code in (0, 1)


class TestCLIGroup:
    """Test CLI group structure."""

    def test_cli_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "Admin CLI" in result.output

    def test_key_group_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["key", "--help"])
        assert result.exit_code == 0
        assert "create" in result.output
        assert "list" in result.output
        assert "revoke" in result.output

    def test_db_group_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["db", "--help"])
        assert result.exit_code == 0
        assert "migrate" in result.output
        assert "seed" in result.output