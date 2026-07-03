from core import account_graph, lifecycle


def test_external_upload_target_label_sub2api_only(monkeypatch):
    monkeypatch.setattr(
        lifecycle,
        "_external_upload_targets_config",
        lambda: {
            "cpa_api_url": "",
            "cpa_api_key": "",
            "sub2api_url": "https://sub2api.example.com",
            "cpa_enabled": False,
            "sub2api_enabled": True,
        },
    )

    assert lifecycle._external_upload_target_label() == "SUB2API"


def test_external_upload_target_label_requires_cpa_key(monkeypatch):
    monkeypatch.setattr(
        lifecycle,
        "_external_upload_targets_config",
        lambda: {
            "cpa_api_url": "https://cpa.example.com",
            "cpa_api_key": "",
            "sub2api_url": "",
            "cpa_enabled": False,
            "sub2api_enabled": False,
        },
    )

    assert lifecycle._external_upload_target_label() == ""


def test_external_upload_target_label_all_enabled(monkeypatch):
    monkeypatch.setattr(
        lifecycle,
        "_external_upload_targets_config",
        lambda: {
            "cpa_api_url": "https://cpa.example.com",
            "cpa_api_key": "secret",
            "sub2api_url": "https://sub2api.example.com",
            "cpa_enabled": True,
            "sub2api_enabled": True,
        },
    )

    assert lifecycle._external_upload_target_label() == "CPA+SUB2API"


def test_k12_account_graph_detects_k12_session():
    graph = {"overview": {"k12_session": {"accessToken": "token"}}, "credentials": []}

    assert lifecycle._is_k12_account_graph(graph) is True


def test_k12_account_graph_detects_workspace_id():
    graph = {"overview": {"k12_workspace_id": "workspace-k12"}, "credentials": []}

    assert lifecycle._is_k12_account_graph(graph) is True


def test_k12_account_graph_detects_plan_type_credential():
    graph = {
        "overview": {},
        "credentials": [
            {"scope": "platform", "key": "plan_type", "value": "k12"},
        ],
    }

    assert lifecycle._is_k12_account_graph(graph) is True


def test_k12_account_graph_ignores_normal_free_account():
    graph = {
        "overview": {},
        "credentials": [
            {"scope": "platform", "key": "plan_type", "value": "free"},
        ],
    }

    assert lifecycle._is_k12_account_graph(graph) is False


def test_k12_html_false_invalid_overview_recovers_display_status():
    overview = {
        "lifecycle_status": "invalid",
        "validity_status": "invalid",
        "display_status": "invalid",
        "plan_state": "free",
        "valid": True,
        "deactivated_reason": "<html><head><title>login</title></head>",
        "chatgpt_usage": {"plan_type": "k12"},
    }

    recovered = account_graph._recover_k12_html_false_invalid_overview(overview, [])

    assert recovered["lifecycle_status"] == "registered"
    assert recovered["validity_status"] == "valid"
    assert recovered["display_status"] == "registered"
    assert recovered["k12_false_invalid_recovered"] is True


def test_k12_html_false_invalid_overview_does_not_recover_invalid_account():
    overview = {
        "lifecycle_status": "invalid",
        "validity_status": "invalid",
        "display_status": "invalid",
        "valid": False,
        "deactivated_reason": "<html><head><title>login</title></head>",
        "chatgpt_usage": {"plan_type": "k12"},
    }

    recovered = account_graph._recover_k12_html_false_invalid_overview(overview, [])

    assert recovered is overview
