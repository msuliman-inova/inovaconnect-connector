import base64
import logging

from odoo import api, models

_logger = logging.getLogger(__name__)


class InovaConnectConnector(models.AbstractModel):
    """Stable adapter interface the hub calls over the External API.

    These are the ONLY methods the hub ever calls to touch this client's
    CRM/Sales/Accounting data - the hub never talks to crm.lead,
    account.move, etc. directly, so it never needs to know (or break when)
    a specific client's CRM has custom required fields or workflows.

    A client with standard, unmodified Odoo gets exactly these default
    implementations for free. A client with custom CRM logic (their own
    lead types, approval workflows, whatever) gets a small override module
    that inherits this model and replaces just the methods it needs to -
    the method names and argument shapes below are the contract; nothing
    about *how* a given client fulfills them is the hub's concern.

    Example override, kept in that client's own custom module:

        class InovaConnectConnector(models.AbstractModel):
            _inherit = "inovaconnect.connector"

            def create_lead(self, name, phone, description=None, extra=None):
                lead = super().create_lead(name, phone, description, extra)
                ... set this client's own custom fields, trigger their
                    approval activity, etc ...
                return lead
    """

    _name = "inovaconnect.connector"
    _description = "InovaConnect Hub Adapter"

    @api.model
    def configure(self, hub_url, tenant_id, shared_secret):
        """Called by the hub itself once it has valid API credentials for
        this client - lets onboarding happen entirely from the hub side,
        instead of also requiring someone to log into this Odoo and
        manually re-type values that already exist on the hub's tenant
        record. Creates or updates this company's inovaconnect.config.
        """
        Config = self.env["inovaconnect.config"].sudo()
        config = Config.search([("company_id", "=", self.env.company.id)], limit=1)
        vals = {
            "hub_url": hub_url,
            "tenant_id": tenant_id,
            "shared_secret": shared_secret,
            "active": True,
        }
        if config:
            config.write(vals)
        else:
            vals["company_id"] = self.env.company.id
            Config.create(vals)
        return True

    @api.model
    def notify_new_message(self, summary=None):
        """Called by the hub the instant a new inbound WhatsApp message
        arrives for this company - the connector holds no chat data at all,
        so without this, staff working in their own Odoo would have no way
        to know a customer message came in unless they already happened to
        have the hub's WhatsApp tab open. Carries a plain text summary only
        (e.g. sender name/number) - never the message body itself, keeping
        this connector as thin as everything else it does.

        Two layers, deliberately: an immediate bus toast for whoever already
        has this Odoo open right now, PLUS a real notification via Odoo's
        own standard mail.thread pipeline (message_notify) - the same one
        @-mentions and activity reminders use - which lands in their Inbox
        AND fires a genuine push notification to any device they've
        registered (desktop browser, installed PWA, or the Odoo mobile
        app), reaching them even with everything closed. Odoo generates its
        own push signing keys automatically; this needs no Firebase/APNs
        setup on our side, only that the person has granted notification
        permission once, same as any other push-enabled site or app.
        """
        users = self.env["res.users"].sudo().search([
            ("company_ids", "in", self.env.company.id),
            ("share", "=", False),
        ])
        if not users:
            return True

        body = summary or "A new WhatsApp message just came in - open InovaConnect to reply."

        for user in users:
            user._bus_send("simple_notification", {
                "type": "info",
                "title": "New WhatsApp message",
                "message": body,
                "sticky": True,
            })

        self.env["mail.thread"].sudo().message_notify(
            subject="New WhatsApp message",
            body=body,
            partner_ids=users.mapped("partner_id").ids,
        )
        return True

    @api.model
    def get_staff_roster(self):
        """Everyone this company's own admin has granted WhatsApp access to
        (Settings > Users > WhatsApp Access), for the hub to sync into its
        hub.staff records. This is the only direction staffing changes flow
        in - a client adds/removes access to their own people here, and the
        hub picks it up next time it syncs; Inova never needs to be told
        about a hire or departure to keep access correct.
        """
        users = self.env["res.users"].sudo().search([
            ("inovaconnect_wa_enabled", "=", True),
            ("active", "=", True),
            ("share", "=", False),
        ])
        return [
            {
                "uid": user.id,
                "login": user.login,
                "name": user.name,
                "role": user.inovaconnect_wa_role or "agent",
            }
            for user in users
        ]

    @api.model
    def create_lead(self, name, phone, description=None, extra=None):
        """Create a CRM lead from a hub-side conversation.

        :param str name: display name for the lead.
        :param str phone: contact phone number.
        :param str description: free-text note, e.g. recent chat history.
        :param dict extra: optional additional crm.lead field values -
            passed through as-is, for callers that know this client's
            schema without the hub needing a fixed field list.
        :return: dict with lead_id and a URL the hub can link out to.
        """
        vals = {
            "name": name or ("WhatsApp - %s" % (phone or "unknown")),
            "type": "opportunity",
            "phone": phone or False,
            "description": description or "",
        }
        if extra:
            vals.update(extra)
        lead = self.env["crm.lead"].sudo().create(vals)
        return {
            "lead_id": lead.id,
            "lead_url": self._record_url(lead),
        }

    @api.model
    def search_contact(self, phone):
        """Find an existing res.partner by phone number.

        :param str phone: phone number in any format (digits extracted).
        :return: dict with partner_id/name/email, or None if not found.
        """
        digits = "".join(c for c in (phone or "") if c.isdigit())
        if len(digits) < 6:
            return None
        suffix = digits[-9:]
        Partner = self.env["res.partner"].sudo()
        domain = [("phone", "ilike", suffix)]
        if "mobile" in Partner._fields:
            domain = ["|", ("mobile", "ilike", suffix)] + domain
        partner = Partner.search(domain, limit=1)
        if not partner:
            return None
        return {
            "partner_id": partner.id,
            "name": partner.name or "",
            "email": partner.email or "",
        }

    @api.model
    def send_document(self, kind, record_id):
        """Render a standard Odoo report as PDF for the hub to attach to a
        WhatsApp message. The client's own Odoo renders it (real logo,
        templates, numbering) - the hub never holds report templates.

        :param str kind: 'invoice' or 'quote' (extend the map below for
            more document types as needed).
        :param int record_id: id of the account.move / sale.order record.
        :return: dict with filename + base64-encoded PDF content, or an
            'error' key if the kind/record isn't available on this client.
        """
        report_map = {
            "invoice": "account.account_invoices",
            "quote": "sale.report_saleorder",
        }
        report_ref = report_map.get(kind)
        if not report_ref:
            return {"error": "Unknown document kind: %s" % kind}
        try:
            pdf_content, _fmt = (
                self.env["ir.actions.report"].sudo()._render_qweb_pdf(report_ref, [record_id])
            )
        except Exception:
            _logger.exception("inovaconnect_connector: failed to render %s for record %s", kind, record_id)
            return {"error": "Could not render this document."}
        return {
            "filename": "%s_%s.pdf" % (kind, record_id),
            "content_base64": base64.b64encode(pdf_content).decode(),
        }

    def _record_url(self, record):
        # The classic /web#model=...&id=... hash route, not the newer
        # /odoo/<slug> path form - the exact slug Odoo expects there varies
        # by version/menu setup, while this hash route has stayed a stable,
        # working deep link across many Odoo versions.
        base_url = self.env["ir.config_parameter"].sudo().get_param("web.base.url", "").rstrip("/")
        return "%s/web#model=%s&id=%s&view_type=form" % (base_url, record._name, record.id)
