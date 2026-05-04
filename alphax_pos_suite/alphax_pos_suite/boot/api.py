"""
AlphaX POS — Unified Boot

The cashier SPA calls `pos_boot(terminal)` exactly once on login. The response
contains everything needed to run the terminal offline-capable for a shift:

    - terminal       : Terminal record (basic identity)
    - outlet         : Outlet record (company, branch, warehouse, price list, ...)
    - domains        : Active domain packs with their capability flags
    - profile        : POS Profile (modes of payment, item groups, tax template)
    - theme          : Theme record if linked
    - loyalty        : Active programs scoped to this outlet's domains
    - payment_methods: Modes of payment + terminal-capture flags
    - scale_rules    : Weighing-scale barcode rules (prefix-based)
    - taxes          : Sales taxes & charges template (rows)
    - currency       : Default currency + symbol + precision
    - server_time    : Server clock (for offline-skew detection)
    - features       : Union of feature flags from active domains

Performance: this is one query-heavy call, but it's ONLY called on login
and on shift open. Caching at frappe.cache layer (per-terminal, 5 min TTL)
makes it cheap on subsequent logins.
"""

import frappe
from frappe import _
from frappe.utils import now_datetime


_FEATURE_FIELDS = [
    "uses_floor_plan",
    "uses_kds",
    "uses_modifiers",
    "uses_recipes",
    "uses_scale",
    "uses_batch_expiry",
    "uses_serial",
    "uses_appointments",
    "uses_tips",
    "uses_service_charge",
    "uses_courses",
    "uses_table_qr",
    "uses_split_bill",
    "uses_loyalty",
    "uses_prescription",
]


def _resolve_outlet_for_terminal(terminal_doc):
    """A Terminal can be linked to an Outlet directly, or via its POS Profile."""
    outlet_name = getattr(terminal_doc, "outlet", None)
    if outlet_name and frappe.db.exists("AlphaX POS Outlet", outlet_name):
        return outlet_name

    profile_name = getattr(terminal_doc, "pos_profile", None)
    if profile_name:
        # The custom field `alphax_outlet` is added to the standard
        # ERPNext POS Profile by our install.py; that's where it lives,
        # not on a separate doctype.
        outlet = frappe.db.get_value(
            "POS Profile", profile_name, "alphax_outlet"
        )
        if outlet and frappe.db.exists("AlphaX POS Outlet", outlet):
            return outlet
    return None


def _domain_pack_summary(domain_code):
    """Return the pack's capability dict, or None if pack missing."""
    if not domain_code:
        return None
    if not frappe.db.exists("AlphaX POS Domain Pack", domain_code):
        return None
    pack = frappe.db.get_value(
        "AlphaX POS Domain Pack",
        domain_code,
        ["domain_code", "label", "icon", "enabled", "default_item_group"]
        + _FEATURE_FIELDS,
        as_dict=True,
    )
    return pack


def _collect_active_domains(outlet_doc):
    """Return list of domain pack summaries for this outlet (in order)."""
    out = []
    seen = set()
    for row in (outlet_doc.domains or []):
        code = row.domain
        if not code or code in seen:
            continue
        seen.add(code)
        summary = _domain_pack_summary(code)
        if summary and summary.get("enabled"):
            out.append(summary)

    if not out:
        legacy = (outlet_doc.pos_type or "").strip()
        if legacy and legacy not in ("Use Global", ""):
            summary = _domain_pack_summary(legacy)
            if summary:
                out.append(summary)

    if not out:
        fallback = _domain_pack_summary("Generic")
        if fallback:
            out.append(fallback)

    return out


def _union_features(domains):
    """Take the OR of every feature flag across active domains."""
    feats = {f: 0 for f in _FEATURE_FIELDS}
    for d in domains:
        for f in _FEATURE_FIELDS:
            if d.get(f):
                feats[f] = 1
    return feats


def _loyalty_programs_for_outlet(outlet_doc, active_domain_codes):
    """All enabled loyalty programs that apply to this outlet."""
    rows = frappe.get_all(
        "AlphaX POS Loyalty Program",
        filters={"enabled": 1, "company": outlet_doc.company},
        fields=[
            "name",
            "program_code",
            "program_name",
            "domain_scope",
            "earn_basis",
            "default_earn_points",
            "default_earn_per_amount",
            "redemption_value",
            "min_points_to_redeem",
            "max_redeem_percent",
            "expiry_days",
        ],
    )
    out = []
    for r in rows:
        scope = r.get("domain_scope") or "All Domains"
        if scope == "All Domains" or scope in active_domain_codes:
            out.append(r)
    return out


def _payment_methods_for_profile(profile_name):
    if not profile_name:
        return []
    rows = frappe.get_all(
        "AlphaX POS Profile Payment Method",
        filters={"parent": profile_name},
        fields=[
            "mode_of_payment",
            "default",
            "amount",
            "allow_in_returns",
        ],
        order_by="idx asc",
    )
    enriched = []
    for r in rows:
        if not r.get("mode_of_payment"):
            continue
        mop = frappe.db.get_value(
            "Mode of Payment",
            r["mode_of_payment"],
            [
                "type",
                "alphax_capture_terminal_data",
                "alphax_terminal_settings",
                "alphax_require_terminal_approval",
                "alphax_allow_manual_ref",
            ],
            as_dict=True,
        ) or {}
        enriched.append({**r, **mop})
    return enriched


def _scale_rules():
    rules = frappe.get_all(
        "AlphaX POS Scale Barcode Rule",
        filters={},
        fields=[
            "name",
            "prefix",
            "total_length",
            "code_start",
            "code_length",
            "value_start",
            "value_length",
            "value_kind",
            "value_divisor",
            "check_digit_present",
        ],
        order_by="prefix",
    )
    return rules


def _taxes_rows(template):
    if not template:
        return []
    return frappe.get_all(
        "Sales Taxes and Charges",
        filters={"parent": template},
        fields=[
            "charge_type",
            "account_head",
            "rate",
            "tax_amount",
            "description",
            "included_in_print_rate",
            "cost_center",
        ],
        order_by="idx asc",
    )


def _company_currency(company):
    if not company:
        return {"currency": "USD", "symbol": "$", "precision": 2}
    cur = frappe.db.get_value("Company", company, "default_currency")
    cur_doc = frappe.db.get_value(
        "Currency", cur, ["symbol", "smallest_currency_fraction_value"], as_dict=True
    ) or {}
    return {
        "currency": cur,
        "symbol": cur_doc.get("symbol") or cur,
        "precision": 2,
    }


@frappe.whitelist()
def pos_boot(terminal):
    """One call returns the entire bootstrap payload."""
    if not terminal or not frappe.db.exists("AlphaX POS Terminal", terminal):
        frappe.throw(_("Terminal not found: {0}").format(terminal))

    cache_key = f"alphax_pos_boot::{terminal}"
    cached = frappe.cache().get_value(cache_key)
    if cached:
        cached["server_time"] = now_datetime().isoformat()
        cached["from_cache"] = True
        return cached

    t = frappe.get_doc("AlphaX POS Terminal", terminal)
    outlet_name = _resolve_outlet_for_terminal(t)

    payload = {
        "terminal": {
            "name": t.name,
            "terminal_name": getattr(t, "terminal_name", t.name),
            "pos_profile": getattr(t, "pos_profile", None),
            "outlet": outlet_name,
        },
        "outlet": None,
        "domains": [],
        "features": {f: 0 for f in _FEATURE_FIELDS},
        "profile": None,
        "theme": None,
        "loyalty_programs": [],
        "payment_methods": [],
        "scale_rules": [],
        "taxes": [],
        "currency": {"currency": "USD", "symbol": "$", "precision": 2},
        "server_time": now_datetime().isoformat(),
        "from_cache": False,
    }

    if outlet_name:
        outlet = frappe.get_doc("AlphaX POS Outlet", outlet_name)
        domains = _collect_active_domains(outlet)
        active_codes = [d["domain_code"] for d in domains]
        payload["outlet"] = {
            "name": outlet.name,
            "outlet_name": outlet.outlet_name,
            "company": outlet.company,
            "branch": outlet.branch,
            "warehouse": outlet.warehouse,
            "cost_center": outlet.cost_center,
            "primary_domain": outlet.primary_domain,
            "update_stock": int(outlet.update_stock or 0),
            "default_price_list": outlet.default_price_list,
            "default_loyalty_program": outlet.default_loyalty_program,
            "service_charge_item": outlet.service_charge_item,
            "tips_item": outlet.tips_item,
            "sales_taxes_and_charges_template": outlet.sales_taxes_and_charges_template,
        }
        payload["domains"] = domains
        payload["features"] = _union_features(domains)
        payload["loyalty_programs"] = _loyalty_programs_for_outlet(outlet, active_codes)
        payload["taxes"] = _taxes_rows(outlet.sales_taxes_and_charges_template)
        payload["currency"] = _company_currency(outlet.company)

    profile_name = getattr(t, "pos_profile", None)
    if profile_name and frappe.db.exists("POS Profile", profile_name):
        prof = frappe.get_doc("POS Profile", profile_name)
        payload["profile"] = {
            "name": prof.name,
            "currency": getattr(prof, "currency", None),
            "language": getattr(prof, "language", None),
            "theme": getattr(prof, "theme", None),
        }
        payload["payment_methods"] = _payment_methods_for_profile(profile_name)
        theme_name = getattr(prof, "theme", None)
        if theme_name and frappe.db.exists("AlphaX POS Theme", theme_name):
            theme = frappe.get_doc("AlphaX POS Theme", theme_name)
            payload["theme"] = theme.as_dict()

    payload["scale_rules"] = _scale_rules()

    frappe.cache().set_value(cache_key, payload, expires_in_sec=300)
    return payload


@frappe.whitelist()
def invalidate_boot_cache(terminal=None):
    """Manager flips an outlet flag, calls this to refresh terminals."""
    if terminal:
        frappe.cache().delete_value(f"alphax_pos_boot::{terminal}")
    else:
        for key in frappe.cache().get_keys("alphax_pos_boot::*"):
            frappe.cache().delete_value(key)
    return {"ok": True}


@frappe.whitelist()
def get_default_terminal_for_session():
    """Resolve which terminal should be auto-selected on this login.

    Resolution order (first match wins):

      1. The ``default_alphax_terminal`` field on the logged-in user.
         (Set by an admin in the User profile under "AlphaX POS" section.)

      2. None — caller falls back to localStorage on the PC, and if
         that's also empty, prompts the cashier to pick a terminal.

    The cashier UI persists the chosen terminal to localStorage on the
    PC, so the second visit on the same browser bypasses this call.
    This server-side default is the FALLBACK when the PC has no memory
    yet (first time setup, browser cache cleared, new device).

    Returns
    -------
    dict with keys:
        terminal      : str | None — terminal ID
        outlet        : str | None — outlet linked to that terminal
        outlet_name   : str | None — display name of outlet
        branch        : str | None — branch linked to that outlet
        can_change    : bool        — true if the user has 'AlphaX POS Manager'
                                       role (used by the UI to show/hide the
                                       'Change Terminal' button)
    """
    user = frappe.session.user
    if not user or user == "Guest":
        return {"terminal": None, "outlet": None, "outlet_name": None,
                "branch": None, "can_change": False}

    # Step 1: read the user's default terminal (custom field added by us)
    terminal = None
    try:
        terminal = frappe.db.get_value("User", user, "default_alphax_terminal")
    except Exception:
        # Custom field not installed yet (mid-migrate, fresh install) —
        # don't crash the cashier; just return no default.
        terminal = None

    # Resolve outlet + branch chain if we got a terminal
    outlet = outlet_name = branch = None
    if terminal:
        # Verify the terminal still exists (the user record could be stale)
        if frappe.db.exists("AlphaX POS Terminal", terminal):
            terminal_doc = frappe.get_cached_doc("AlphaX POS Terminal", terminal)
            try:
                outlet = _resolve_outlet_for_terminal(terminal_doc)
            except Exception:
                outlet = None
            if outlet and frappe.db.exists("AlphaX POS Outlet", outlet):
                outlet_doc = frappe.get_cached_doc("AlphaX POS Outlet", outlet)
                outlet_name = outlet_doc.get("outlet_name") or outlet
                branch = outlet_doc.get("branch") or None
        else:
            terminal = None  # stale link — fall through to "no default"

    # Manager check — used by the UI to show/hide the "Change" button
    can_change = (
        "AlphaX POS Manager" in frappe.get_roles(user)
        or "System Manager" in frappe.get_roles(user)
    )

    return {
        "terminal": terminal,
        "outlet": outlet,
        "outlet_name": outlet_name,
        "branch": branch,
        "can_change": can_change,
    }


@frappe.whitelist()
def list_terminals_for_picker():
    """Return all active terminals with their outlet/branch context.

    Used by the cashier UI's first-time terminal picker dialog (when
    no default exists for the user and the PC has no localStorage).

    The list is intentionally NOT filtered by user permissions — the
    point is "let the cashier or manager pick the right station". POS
    Profile permissions are still enforced at order creation time, so
    this is safe.
    """
    rows = frappe.get_all(
        "AlphaX POS Terminal",
        fields=["name", "pos_outlet"],
        order_by="name asc",
        limit_page_length=200,
    )

    # Enrich with outlet/branch names so the picker can show
    # "Riyadh Mall — Branch 02 — Terminal 3" in the dropdown.
    out = []
    outlet_cache = {}
    for r in rows:
        outlet = r.get("pos_outlet")
        outlet_name = branch = None
        if outlet:
            if outlet not in outlet_cache:
                if frappe.db.exists("AlphaX POS Outlet", outlet):
                    od = frappe.get_cached_doc("AlphaX POS Outlet", outlet)
                    outlet_cache[outlet] = (
                        od.get("outlet_name") or outlet,
                        od.get("branch") or None,
                    )
                else:
                    outlet_cache[outlet] = (outlet, None)
            outlet_name, branch = outlet_cache[outlet]

        out.append({
            "terminal": r["name"],
            "outlet": outlet,
            "outlet_name": outlet_name,
            "branch": branch,
        })
    return out
