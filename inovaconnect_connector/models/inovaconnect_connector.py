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
