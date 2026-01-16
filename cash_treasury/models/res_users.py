from odoo import models, fields, api

class ResUsers(models.Model):
    _inherit = "res.users"

    cash_treasury_journal_ids = fields.Many2many(
        "account.journal",
        "res_users_cash_treasury_journal_rel",
        "user_id",
        "journal_id",
        string="Cash Treasury Journals",
        domain="[('type','in',('cash','bank'))]"
    )

    def write(self, vals):
        res = super().write(vals)
        if 'cash_treasury_journal_ids' in vals:
           
            try:
                self.env['ir.rule'].clear_caches()
                self.env.registry.clear_cache()
                
              
                if hasattr(self.env['account.journal'], '_cache'):
                    self.env['account.journal']._cache.clear()
                
              
                if 'cash.treasury.in' in self.env:
                    self.env['cash.treasury.in'].clear_caches()
                if 'cash.treasury.out' in self.env:
                    self.env['cash.treasury.out'].clear_caches()
                    
            except Exception:
                pass
        return res