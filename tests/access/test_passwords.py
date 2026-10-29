import pytest

from ddm.access.passwords import hash_password, verify_password


def test_a_hash_verifies_its_own_password_only():
    stored = hash_password("correct horse battery")

    assert stored.startswith("scrypt$16384$8$1$")
    assert verify_password("correct horse battery", stored)
    assert not verify_password("correct horse batterz", stored)


def test_the_same_password_hashes_differently_each_time():
    assert hash_password("same password!") != hash_password("same password!")


@pytest.mark.parametrize(
    "stored",
    [
        "garbage",
        "",
        "bcrypt$16384$8$1$AAAA$AAAA",
        "scrypt$x$8$1$AAAA$AAAA",
        "scrypt$1048576$8$1$AAAAAAAAAAAAAAAAAAAAAA==$" + "A" * 44,  # n above the cap
        "scrypt$16383$8$1$AAAAAAAAAAAAAAAAAAAAAA==$" + "A" * 44,  # n not a power of two
        "scrypt$16384$8$1$AAAAAAAAAAAAAAAAAAAAAA==$AAAA",  # wrong hash length
        "scrypt$16384$8$1$!!!$!!!",
    ],
)
def test_a_malformed_stored_value_never_verifies_and_never_raises(stored):
    assert verify_password("anything at all", stored) is False


def test_parameters_beyond_the_memory_limit_do_not_raise():
    stored = "scrypt$32768$16$4$AAAAAAAAAAAAAAAAAAAAAA==$" + "A" * 43 + "="

    assert verify_password("anything at all", stored) is False
