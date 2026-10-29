from ddm.query.fields import OTHER_FIELDS, PUBLIC_ATTRS, changed_fields, public_attrs

EMAIL = "jane.secret@example.org"


def test_public_attrs_keeps_only_whitelisted_keys():
    attrs = {"language": "en", "owner_email": EMAIL, "editor_email": EMAIL, "website": "https://a"}

    assert public_attrs(2, attrs) == {"language": "en", "website": "https://a"}


def test_public_attrs_returns_a_new_dict():
    attrs = {"language": "en"}

    result = public_attrs(1, attrs)

    assert result == attrs and result is not attrs


def test_public_attrs_of_an_unknown_source_is_empty():
    assert public_attrs(99, {"language": "en"}) == {}


def test_no_source_whitelists_an_email_field():
    for keys in PUBLIC_ATTRS.values():
        assert not any("email" in key for key in keys)


def test_every_source_has_a_whitelist():
    assert set(PUBLIC_ATTRS) == {1, 2, 3, 4, 5}


def test_changed_fields_names_whitelisted_keys_in_order():
    before = {"language": "en", "episode_count": 3}
    after = {"language": "fr", "episode_count": 4, "author": "A"}

    assert changed_fields(2, before, after) == ("author", "episode_count", "language")


def test_changed_fields_folds_non_public_keys_into_one_entry():
    before = {"owner_email": "a@example.org"}
    after = {"owner_email": EMAIL}

    assert changed_fields(2, before, after) == (OTHER_FIELDS,)


def test_changed_fields_with_nothing_changed_is_empty():
    assert changed_fields(1, {"language": "en"}, {"language": "en"}) == ()


def test_changed_fields_counts_a_removed_key():
    assert changed_fields(5, {"outlet": "X", "bylines": []}, {"outlet": "X"}) == ("bylines",)
