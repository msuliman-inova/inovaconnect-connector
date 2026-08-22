import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

try:
    import requests
except ImportError:
    requests = None

_logger = logging.getLogger(__name__)


class InovaConnectConfig(models.Model):
    _name = "inovaconnect.config"
    _description = "InovaConnect Hub Connection Settings"

    name = fields.Char(default="InovaConnect", required=True)
    company_id = fields.Many2one(
        "res.company", string="Company", required=True, index=True,
        default=lambda self: self.env.company,
    )
    hub_url = fields.Char(
        string="Hub URL", required=True,
        help="Base URL of the InovaConnect hub, e.g. https://hub.inova.technology "
             "(provided by Inova when this company is onboarded).",
    )
    tenant_id = fields.Char(
        string="Tenant ID", required=True,
        help="Identifier assigned by Inova when this company was onboarded.",
    )
    shared_secret = fields.Char(
        string="Shared Secret", required=True,
        help="Used to sign the SSO handoff token so the hub can trust who's "
             "asking - never share this outside Inova and this company's admins.",
        groups="base.group_system",
    )
    active = fields.Boolean(default=True)

    # -- Cached copy of the hub's current maintenance notice, if any --
    # Cached rather than fetched live on every form view: showing "the hub
    # will be down soon" should still work even if the hub is briefly
    # unreachable at that exact moment, which live-fetching would defeat.
    maintenance_message = fields.Text(readonly=True)
    maintenance_scheduled_at = fields.Datetime(readonly=True)
    maintenance_duration_minutes = fields.Integer(readonly=True)
    maintenance_fetched_at = fields.Datetime(readonly=True)
    maintenance_notice_current = fields.Boolean(
        compute="_compute_maintenance_notice_current",
        help="Whether the cached maintenance notice is still within its window.",
    )

    _company_unique = models.Constraint(
        "UNIQUE(company_id)",
        "This company already has an InovaConnect configuration.",
    )

    @api.depends("maintenance_message", "maintenance_scheduled_at", "maintenance_duration_minutes")
    def _compute_maintenance_notice_current(self):
        # Re-checked on every read rather than trusting the cache forever -
        # if the periodic refresh ever stops running, an old notice
        # shouldn't linger on screen indefinitely.
        now = fields.Datetime.now()
        for config in self:
            if not (config.maintenance_message and config.maintenance_scheduled_at):
                config.maintenance_notice_current = False
                continue
            window_end = config.maintenance_scheduled_at + timedelta(
                minutes=config.maintenance_duration_minutes or 0
            )
            config.maintenance_notice_current = now <= window_end

    def fetch_maintenance_notice(self):
        """Poll the hub for its current maintenance notice and cache the
        result locally. Failures are logged and swallowed, never raised -
        this runs unattended on a cron and a hub that's briefly
        unreachable shouldn't be treated as an error.

        A notice that's new (or changed) since the last poll is also
        broadcast as a real-time toast to every internal user of this
        company - the config screen's banner only reaches whoever happens
        to be looking at it, which for most companies is nobody most of
        the time. This is what actually gets it in front of people.
        """
        if not requests:
            return
        for config in self:
            if not config.hub_url:
                continue
            try:
                resp = requests.get(
                    "%s/inovaconnect/maintenance-notice" % config.hub_url.rstrip("/"),
                    timeout=10,
                )
                resp.raise_for_status()
                notice = resp.json().get("notice")
            except Exception:
                _logger.warning(
                    "inovaconnect_connector: could not reach hub for maintenance notice", exc_info=True
                )
                continue

            previous_message = config.maintenance_message
            previous_scheduled_at = config.maintenance_scheduled_at

            if notice:
                config.write({
                    "maintenance_message": notice.get("message"),
                    "maintenance_scheduled_at": notice.get("scheduled_at"),
                    "maintenance_duration_minutes": notice.get("duration_minutes"),
                    "maintenance_fetched_at": fields.Datetime.now(),
                })
                is_new = (
                    notice.get("message") != previous_message
                    or notice.get("scheduled_at") != fields.Datetime.to_string(previous_scheduled_at)
                )
                if is_new:
                    config._broadcast_maintenance_notice(notice)
            else:
                config.write({
                    "maintenance_message": False,
                    "maintenance_scheduled_at": False,
                    "maintenance_duration_minutes": False,
                    "maintenance_fetched_at": fields.Datetime.now(),
                })

    def _broadcast_maintenance_notice(self, notice):
        """Push a real-time toast to every internal (non-portal) user of
        this company - reuses Odoo's own built-in 'simple_notification'
        bus channel, so it needs no custom frontend code and shows up in
        any Odoo tab someone has open, not just InovaConnect's screens."""
        self.ensure_one()
        users = self.env["res.users"].sudo().search([
            ("company_ids", "in", self.company_id.id),
            ("share", "=", False),
        ])
        for user in users:
            user._bus_send("simple_notification", {
                "type": "warning",
                "title": "InovaConnect maintenance notice",
                "message": notice.get("message") or "",
                "sticky": True,
            })

    @api.model
    def _cron_fetch_maintenance_notices(self):
        self.sudo().search([("active", "=", True)]).fetch_maintenance_notice()

    def _build_sso_token(self):
        """Build a short-lived, signed identity claim for the SSO handoff.

        Deliberately carries identity only (tenant, uid) - never a role or
        permission. The hub resolves the caller's actual role by calling
        back into this same Odoo, so a permission change here takes effect
        immediately instead of waiting for a stale token to expire, and a
        leaked secret can't be used to mint an elevated role directly.
        """
        self.ensure_one()
        # sudo(): shared_secret is restricted to base.group_system at the
        # field level (so it doesn't show in the form for regular agents),
        # but every agent needs to be able to trigger this action - the
        # signing itself has to work regardless of who's clicking the button.
        config = self.sudo()
        if not (config.hub_url and config.tenant_id and config.shared_secret):
            raise UserError(_(
                "InovaConnect is not fully configured for this company. "
                "Please set the Hub URL, Tenant ID and Shared Secret."
            ))
        claims = {
            "tenant_id": config.tenant_id,
            "odoo_uid": self.env.user.id,
            "odoo_login": self.env.user.login,
            "company_id": config.company_id.id,
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
            "nonce": secrets.token_hex(16),
        }
        payload = json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
        signature = hmac.new(
            config.shared_secret.encode(), payload, hashlib.sha256
        ).hexdigest()
        token = base64.urlsafe_b64encode(payload).decode() + "." + signature
        return token

    def action_open_inovaconnect(self):
        """Redirect the current user into the hub, pre-authenticated."""
        self.ensure_one()
        token = self._build_sso_token()
        url = "%s/sso?token=%s" % (self.hub_url.rstrip("/"), token)
        return {
            "type": "ir.actions.act_url",
            "url": url,
            "target": "new",
        }

    @api.model
    def get_active_config(self):
        """Return this company's active config, or False if unset."""
        return self.search([
            ("company_id", "=", self.env.company.id),
            ("active", "=", True),
        ], limit=1)

    @api.model
    def action_open_inovaconnect_for_company(self):
        """Menu-callable entry point for any internal user - the config
        record itself (and its secret) stays admin-only, but everyone
        needs to be able to trigger the actual sign-in, not just whoever
        can see the settings screen."""
        config = self.get_active_config()
        if not config:
            raise UserError(_(
                "InovaConnect isn't configured for this company yet - "
                "contact your administrator."
            ))
        return config.action_open_inovaconnect()
