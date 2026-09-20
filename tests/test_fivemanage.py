from unittest.mock import Mock, patch

from nerp_forms_bot.fivemanage import FiveManageService


def test_upload_png_uses_token_only_as_authorization_header() -> None:
    response = Mock()
    response.json.return_value = {
        "status": "ok",
        "data": {"url": "https://cdn.example.test/warrant.png"},
    }

    with patch("nerp_forms_bot.fivemanage.requests.post", return_value=response) as post:
        result = FiveManageService("protected-token", "nerp-doj/court-orders").upload_png(
            filename="arrest-warrant-cor-000001.png",
            content=b"png-content",
            request_id="COR-000001",
        )

    assert result.url == "https://cdn.example.test/warrant.png"
    assert post.call_args.kwargs["headers"] == {"Authorization": "protected-token"}
    assert post.call_args.kwargs["data"]["path"] == "nerp-doj/court-orders"
    assert post.call_args.kwargs["files"]["file"][2] == "image/png"
