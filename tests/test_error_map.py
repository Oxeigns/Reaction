from pyrogram import errors

from bot.error_map import map_exc


def test_map_flood_wait():
    err = errors.FloodWait(10, "wait")
    info = map_exc(err)
    assert info.code == "FLOOD_WAIT"
    assert info.retry_after == 10


def test_map_invite_invalid():
    err = errors.InviteHashInvalid("invalid")
    info = map_exc(err)
    assert info.code == "INVITE_INVALID_HASH"
    assert info.detail == "Invite hash invalid"


def test_map_unknown():
    class CustomError(Exception):
        pass

    info = map_exc(CustomError("boom"))
    assert info.code == "UNKNOWN_ERROR"
    assert "boom" in info.detail
