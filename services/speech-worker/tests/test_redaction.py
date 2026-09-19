"""Public error redaction tests."""

from speech_capture_worker.redaction import (
    public_cli_error_payload,
    public_error_message,
)


def test_remote_error_text_is_derived_only_from_stable_code() -> None:
    assert public_error_message("SOURCE_UNDECODABLE") == (
        "The uploaded source could not be decoded as supported audio."
    )
    assert public_error_message("PRIVATE_BACKEND_FAILED") == (
        "The Worker could not complete this processing stage safely."
    )


def test_local_cli_error_keeps_action_but_redacts_paths_and_credentials() -> None:
    private_path = "/Users/private/customer/meeting.wav"
    token = "scw_abcdefghijklmnopqrstuvwxyz012345"
    payload = public_cli_error_payload(
        "INVALID_JOB_REQUEST",
        f"Model revision is invalid for {private_path} using {token}.",
    )

    assert payload["code"] == "INVALID_JOB_REQUEST"
    assert "revision" in payload["message"]
    assert private_path not in payload["message"]
    assert token not in payload["message"]
    assert "[redacted-path]" in payload["message"]
    assert "[redacted-credential]" in payload["message"]
