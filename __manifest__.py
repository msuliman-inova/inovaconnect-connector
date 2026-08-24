{
    'name': 'InovaConnect Connector',
    'version': '19.0.1.0.0',
    'summary': 'Thin SSO link + CRM adapter connecting this Odoo to the InovaConnect hub',
    'description': """
InovaConnect Connector
=======================

Installed on a CLIENT's own Odoo. Deliberately thin: it holds no chat data,
no message history, no AI logic - all of that lives only in the InovaConnect
hub, which this client's staff never has direct access to.

What this module actually does:
--------------------------------
1. Stores the hub URL, tenant id and a shared signing secret for this
   company (Configuration > InovaConnect).
2. "Open InovaConnect" builds a short-lived signed token identifying the
   current user and redirects them into the hub - the hub verifies the
   signature and looks up this user's real role by calling back into this
   same Odoo, so permissions are never duplicated/out of sync.
3. Exposes a small, stable adapter interface (inovaconnect.connector) that
   the hub calls over the External API whenever it needs to touch this
   client's CRM/Sales/Accounting data - create_lead, search_contact,
   send_document. The default implementations here do the plain, standard
   thing; a client with custom CRM workflows gets their own override module
   inheriting inovaconnect.connector, without the hub ever needing to know
   the difference.
""",
    'author': 'InovaTech',
    'website': 'https://www.inova.technology/',
    'support': 'info@inova.technology',
    'category': 'Extra Tools',
    'license': 'OPL-1',
    'depends': ['base', 'bus', 'mail', 'crm'],
    'external_dependencies': {
        'python': ['requests'],
    },
    'data': [
        'security/inovaconnect_groups.xml',
        'security/ir.model.access.csv',
        'views/inovaconnect_config_views.xml',
        'views/res_users_views.xml',
        'data/ir_cron_data.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
