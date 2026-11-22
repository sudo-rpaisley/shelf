"""Invalid personal-state values remain diagnostic text, never reflected HTML."""

from tests.conftest import _insert_item


def test_quick_status_invalid_value_is_plain_text_and_writes_no_personal_state(editor_client, editor_user, db):
    item = _insert_item(db, isbn=None)
    db.commit()
    before = tuple(db.iterdump())
    value = "<b>not-a-status</b>"
    response = editor_client.post(f"/api/items/{item}/reading-status", data={"status": value})
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == f"Invalid reading status: {value!r}"
    assert tuple(db.iterdump()) == before
    assert db.execute("SELECT 1 FROM user_item_state WHERE user_id=? AND item_id=?",
                      (editor_user["id"], item)).fetchone() is None
