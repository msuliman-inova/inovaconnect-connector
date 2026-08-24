from odoo import fields, models


class ResUsers(models.Model):
    _inherit = "res.users"

    inovaconnect_wa_enabled = fields.Boolean(
        string="WhatsApp Access",
        help="Grants this person access to WhatsApp chat via InovaConnect. "
             "Managed here by this company's own admin - the hub picks up "
             "changes the next time it syncs, no need to contact Inova to "
             "add or remove someone.",
    )
    inovaconnect_wa_role = fields.Selection(
        [("agent", "Agent"), ("supervisor", "Supervisor"), ("manager", "Manager")],
        string="WhatsApp Role", default="agent",
        help="Agent: can chat on assigned conversations. Supervisor: also "
             "sees and assigns all conversations. Manager: also manages "
             "WhatsApp numbers, templates and configuration.",
    )
