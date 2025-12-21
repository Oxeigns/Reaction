from bot.link_parser import normalize_url, parse_join_link, parse_message_link


def test_normalize_removes_query_and_tg_resolve():
    url = normalize_url("tg://resolve?domain=test&start=12&foo=bar")
    assert url == "https://t.me/test/12"


def test_parse_join_invite_hash():
    link = parse_join_link("https://t.me/+abcdef")
    assert link
    assert link.kind == "invite_hash"
    assert link.value == "abcdef"


def test_parse_join_public_username():
    link = parse_join_link("t.me/publicgroup")
    assert link
    assert link.kind == "public_username"
    assert link.value == "publicgroup"


def test_parse_message_public():
    link = parse_message_link("https://t.me/example/123")
    assert link
    assert link.kind == "public_msg"
    assert link.chat_ref == "example"
    assert link.msg_id == 123


def test_parse_message_private_c():
    link = parse_message_link("t.me/c/123456/45")
    assert link
    assert link.kind == "private_c_msg"
    assert link.chat_ref == -100123456
    assert link.msg_id == 45


def test_parse_message_rejects_profile():
    assert parse_message_link("https://t.me/someuser") is None
