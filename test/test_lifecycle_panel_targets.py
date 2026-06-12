from core import lifecycle


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
