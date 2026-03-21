from __future__ import annotations

import pytest

from tests.datacloud_test_utils import (
    CLI_HELP_COMMANDS,
    ORG_ALIAS,
    output_text,
    run,
    sf_data360_available,
)

pytestmark = pytest.mark.integration



def _require_runtime() -> None:
    if not sf_data360_available():
        pytest.skip("sf data360 runtime is not installed; run bootstrap-plugin.sh first")



def _require_org_alias() -> str:
    if not ORG_ALIAS:
        pytest.skip("Set SF_DATACLOUD_ORG_ALIAS to run org-backed Data Cloud integration tests")
    return ORG_ALIAS



def test_all_curated_datacloud_cli_commands_exist_in_installed_runtime() -> None:
    _require_runtime()

    for command in CLI_HELP_COMMANDS:
        result = run(command, timeout=180)
        assert result.returncode == 0, output_text(result)



def test_verify_plugin_script_allows_partial_org_feature_gating() -> None:
    _require_runtime()
    org_alias = _require_org_alias()

    result = run(["bash", "skills/sf-datacloud/scripts/verify-plugin.sh", org_alias], timeout=300)
    output = output_text(result)

    assert result.returncode == 0, output
    assert "sf data360 runtime detected" in output
    assert f"org alias '{org_alias}' is authenticated" in output
    assert "Verification complete." in output



def test_org_backed_smoke_matrix_covers_each_datacloud_phase() -> None:
    _require_runtime()
    org_alias = _require_org_alias()

    smoke_cases = [
        {
            "label": "orchestrator-doctor",
            "command": ["sf", "data360", "doctor", "-o", org_alias],
            "accepted": [
                "Request failed",
                "This feature is not currently enabled for this user type or org",
                "Couldn't find CDP tenant ID",
            ],
        },
        {
            "label": "connect-connector-list",
            "command": ["sf", "data360", "connection", "connector-list", "-o", org_alias],
            "accepted": ["AdobeMarketoEngage", "SalesforceCRM", "Databricks"],
        },
        {
            "label": "prepare-data-stream-list",
            "command": ["sf", "data360", "data-stream", "list", "-o", org_alias],
            "accepted": [
                "No results.",
                "This feature is not currently enabled for this user type or org: [CdpDataStreams]",
            ],
        },
        {
            "label": "harmonize-identity-resolution-list",
            "command": ["sf", "data360", "identity-resolution", "list", "-o", org_alias],
            "accepted": [
                "No results.",
                "This feature is not currently enabled for this user type or org: [CdpIdentityResolution]",
            ],
        },
        {
            "label": "segment-calculated-insight-list",
            "command": ["sf", "data360", "calculated-insight", "list", "-o", org_alias],
            "accepted": ["No results.", "Name"],
        },
        {
            "label": "act-activation-platforms",
            "command": ["sf", "data360", "activation", "platforms", "-o", org_alias],
            "accepted": [
                "This feature is not currently enabled for this user type or org: [CdpActivationExternalPlatform]",
                "Platform",
            ],
        },
        {
            "label": "retrieve-query-describe",
            "command": [
                "sf",
                "data360",
                "query",
                "describe",
                "-o",
                org_alias,
                "--table",
                "ssot__Individual__dlm",
            ],
            "accepted": [
                "Couldn't find CDP tenant ID. Please enable CDP first.",
                "Field Name",
                "No results.",
            ],
        },
    ]

    for case in smoke_cases:
        result = run(case["command"], timeout=300)
        output = output_text(result)
        assert any(fragment in output for fragment in case["accepted"]) or result.returncode == 0, (
            f"Unexpected smoke output for {case['label']}:\n{output}"
        )
