"""Step definitions for write access control scenarios.

Tests department-based ACL with a scoped (non-admin) user. The scoped
user has allowed_departments=['Product'], so Product vendors are editable
and Engineering vendors are denied.

Shared Given/Then steps (ACL test vendors exist, denial assertions, etc.)
live in step_defs/conftest.py.
"""

from pytest_bdd import scenarios, when, parsers

from tests.conftest import (
    SCOPED_HEADERS,
    chat_with_csv,
    make_csv,
    scoped_chat,
)

scenarios("../features/write_access_control.feature")


# ── When — scoped CSV uploads ────────────────────────────────────────────


@when(
    parsers.re(
        r'the scoped user uploads a CSV changing purpose for "(?P<vendor_a>[^"]+)" and "(?P<vendor_b>[^"]+)"'
    ),
    target_fixture="context",
)
def scoped_csv_mixed(acl_vendor_ids, vendor_a, vendor_b):
    csv = make_csv(
        ["id", "purpose"],
        [
            [acl_vendor_ids[vendor_a], "Updated purpose"],
            [acl_vendor_ids[vendor_b], "Should be blocked"],
        ],
    )
    result = chat_with_csv(csv, prompt="Process this vendor CSV", headers=SCOPED_HEADERS)
    return {"result": result}


@when(
    parsers.re(
        r'the scoped user uploads a CSV changing purpose for "(?P<vendor>[^"]+)"$'
    ),
    target_fixture="context",
)
def scoped_csv_single(acl_vendor_ids, vendor):
    vid = acl_vendor_ids[vendor]
    csv = make_csv(["id", "purpose"], [[vid, "Updated purpose"]])
    result = chat_with_csv(csv, prompt="Process this vendor CSV", headers=SCOPED_HEADERS)
    return {"result": result}
