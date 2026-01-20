from odoo import models, fields, api, _
from odoo.exceptions import ValidationError, UserError
from odoo.tools.float_utils import float_compare

# =====================================================
# ALLOCATION LINE
# =====================================================

class CashTreasuryOutMultiAccountLine(models.Model):
    _name = 'cash.treasury.out.multi.account.line'
    _description = 'Cash Treasury Out Multi Account Line'

    cash_out_id = fields.Many2one(
        'cash.treasury.out',
        string="Cash Out",
        required=True,
        ondelete="cascade"
    )

    account_id = fields.Many2one(
        'account.account',
        string="Account",
        required=True
    )

    amount = fields.Float(
        string="Amount",
        required=True
    )

    notes = fields.Char(
        string="Notes"
    )


# =====================================================
# CASH OUT
# =====================================================
class CashTreasuryOut(models.Model):
    _name = "cash.treasury.out"
    _description = "Cash Out"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "id desc"

    # -------------------------
    # BASIC FIELDS
    # -------------------------
    name = fields.Char(readonly=True, copy=False)

    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("reviewed", "Reviewed"),
            ("approved", "Approved (Ready to Pay)"),
            ("paid", "Paid"),
        ],
        default="draft",
        tracking=True,
    )

    pay_to_type = fields.Selection(
        [
            ("account", "Account"),
            ("partner", "Partner"),
        ],
        required=True,
        default="account",
    )
    
    multi_account = fields.Boolean(string="Multi Account", default=False)

    multi_account_line_ids = fields.One2many(
        'cash.treasury.out.multi.account.line',
        'cash_out_id',
        string="Multi Accounts",
        copy=False
    )
    

    partner_id = fields.Many2one("res.partner")
    account_id = fields.Many2one("account.account")
    
    
    amount_manual = fields.Monetary(
        string="Amount",
        currency_field="currency_id",
        default=0.0,
        required=False,
    )


    amount = fields.Monetary(
        string="Amount",
        compute="_compute_amount",
        inverse="_inverse_amount",
        store=True,
        required=True,
    )
    date = fields.Date(default=fields.Date.context_today, required=True, tracking=True)
    notes = fields.Text()

    # New: actual payment date (used for posting)
    payment_date = fields.Date(tracking=True)

    journal_id = fields.Many2one(
        "account.journal",
        required=True,
        domain=lambda self: self._get_journal_domain(),
    )

    payment_method_id = fields.Many2one(
        "account.payment.method",
        required=True,
        domain="[('payment_type','=','outbound')]",
    )

    company_id = fields.Many2one(
        "res.company",
        default=lambda self: self.env.company,
        required=True,
    )

    currency_id = fields.Many2one(
        "res.currency",
        related="company_id.currency_id",
        readonly=True,
    )

    destination_account_id = fields.Many2one(
        "account.account",
        compute="_compute_destination_account",
        store=True,
    )
    
    destination_accounts_text = fields.Char(
        string="Destination Accounts",
        compute="_compute_destination_accounts_text",
        store=False,
    )

    # -------------------------
    # ALLOCATION
    # -------------------------
    bills_loaded = fields.Boolean(default=False, copy=False)

    allocation_line_ids = fields.One2many(
        "cash.treasury.out.allocation",
        "cash_out_id",
        copy=False,
    )

    total_allocated = fields.Monetary(
        compute="_compute_totals",
        currency_field="currency_id",
    )

    allocation_diff = fields.Monetary(
        compute="_compute_totals",
        currency_field="currency_id",
    )

    journal_entry_id = fields.Many2one("account.move", readonly=True)
    reversal_entry_id = fields.Many2one("account.move", readonly=True, copy=False)

    # =================================================
    # DOMAIN METHOD FOR JOURNAL FILTERING
    # =================================================
    def _get_journal_domain(self):
        """Filter journals based on user permissions"""
        try:
            # ÌíÈ ÇáíæÒÑ Çááí ÚÇãá action
            user = self.env.user
            
            # Super users (Admin) see all cash/bank journals
            if user.has_group('base.group_system'):
                return [('type', 'in', ('cash', 'bank'))]
            
            # Regular users see only journals they have permission for
            journal_ids = user.cash_treasury_journal_ids.ids
            
            # If no journals assigned, show none
            if not journal_ids:
                return [('id', '=', False)]  # Empty domain
            
            return [('id', 'in', journal_ids), ('type', 'in', ('cash', 'bank'))]
        
        except Exception as e:
            # Fallback if something goes wrong
            import logging
            logging.error("Journal domain error: %s", str(e))
            return [('type', 'in', ('cash', 'bank'))]

    # =================================================
    # ONCHANGE FOR JOURNAL
    # =================================================
    @api.onchange('journal_id')
    def _onchange_journal_id(self):
        """Verify user has permission for selected journal"""
        # Admin can select any journal without warning
        if self.env.user.has_group('base.group_system'):
            return  # ÇáÜ Admin íãÔí ÈÏæä ÊÍÐíÑ
        
        # For non-admin users: check permissions
        if self.journal_id and self.env.user.id:
            user_journals = self.env.user.cash_treasury_journal_ids.ids
            if self.journal_id.id not in user_journals:
                warning = {
                    'title': 'Permission Denied',
                    'message': 'You do not have permission to use this journal. Please select from your allowed journals.'
                }
                self.journal_id = False
                return {'warning': warning}

    # =================================================
    # HARD LOCK (ALLOW WORKFLOW ONLY)
    # =================================================
    def write(self, vals):
        """
        Rules:
        - Draft: editable by Cash Entry (ACL controls)
        - Reviewed/Approved/Paid: read-only (except workflow fields)
        - Approved: Cash Entry can set payment_date only (and nothing else)
        - Super Approver: can bypass lock (used for cancel paid -> draft)
        """
        # Super Approver bypass
        if self.env.user.has_group("cash_treasury.group_cash_super_approver"):
            return super().write(vals)

        allowed_with_state = {
            "state",
            "payment_date",
            "journal_entry_id",
            "reversal_entry_id",
            "name",
            # chatter / technical
            "write_date",
            "message_ids",
            "message_follower_ids",
        }

        allowed_no_state = {
            "journal_entry_id",
            "reversal_entry_id",
            "name",
            "write_date",
            "message_ids",
            "message_follower_ids",
        }

        for rec in self:
            # Allow Payment Date edit in Approved for Cash Entry only
            if rec.state == "approved":
                if (
                    self.env.user.has_group("cash_treasury.group_cash_entry")
                    and set(vals.keys()) == {"payment_date"}
                ):
                    continue

            # Workflow writes (buttons) can update state + related technical links
            if rec.state != "draft":
                if "state" in vals:
                    if not set(vals.keys()).issubset(allowed_with_state):
                        raise UserError("Modification is only allowed in Draft state.")
                else:
                    # allow only technical/chatter updates without state
                    if not set(vals.keys()).issubset(allowed_no_state):
                        raise UserError("Modification is only allowed in Draft state.")

        return super().write(vals)

    def unlink(self):
        for rec in self:
            if rec.state != "draft":
                raise UserError("You can only delete a Cash Out document in Draft state.")
        return super().unlink()

    # =================================================
    # COMPUTES
    # =================================================
    @api.depends("pay_to_type", "account_id", "partner_id")
    def _compute_destination_account(self):
        for rec in self:
            if rec.pay_to_type == "account":
                rec.destination_account_id = rec.account_id
            else:
                rec.destination_account_id = (
                    rec.partner_id.property_account_payable_id
                    if rec.partner_id
                    else False
                )

    @api.depends("amount_manual", "multi_account", "multi_account_line_ids.amount", "allocation_line_ids.amount_to_pay")
    def _compute_totals(self):
        for rec in self:
            total = sum(l.amount_to_pay or 0.0 for l in rec.allocation_line_ids)
            rec.total_allocated = total
            rec.allocation_diff = (rec.amount or 0.0) - total
    

    @api.depends(
        "multi_account",
        "multi_account_line_ids.account_id",
        "multi_account_line_ids.amount",
        "destination_account_id",
    )
    def _compute_destination_accounts_text(self):
        for rec in self:
            if rec.multi_account:
                parts = []
                for line in rec.multi_account_line_ids:
                    if line.account_id:
                        parts.append(
                            f"{line.account_id.code or ''} {line.account_id.name} ({line.amount or 0.0})"
                        )

                if not parts:
                    rec.destination_accounts_text = False
                else:
                    # show first 2 only
                    if len(parts) > 2:
                        rec.destination_accounts_text = " | ".join(parts[:2]) + " | ..."
                    else:
                        rec.destination_accounts_text = " | ".join(parts)

            else:
                if rec.destination_account_id:
                    rec.destination_accounts_text = (
                        f"{rec.destination_account_id.code or ''} {rec.destination_account_id.name}"
                    )
                else:
                    rec.destination_accounts_text = False


    # =================================================
    # ONCHANGE
    # =================================================
    @api.onchange("pay_to_type")
    def _onchange_pay_to_type(self):
        if self.pay_to_type == "account":
            self.partner_id = False
            self.bills_loaded = False
            self.allocation_line_ids = [(5, 0, 0)]
        else:
            self.account_id = False

    @api.onchange("partner_id")
    def _onchange_partner(self):
        if self.bills_loaded:
            self.bills_loaded = False
            self.allocation_line_ids = [(5, 0, 0)]
            
    @api.onchange("multi_account")
    def _onchange_multi_account(self):
        if self.multi_account:
            self.pay_to_type = "account"
            self.partner_id = False
            self.account_id = False
            self.amount_manual = 0.0
            self.bills_loaded = False
            self.allocation_line_ids = [(5, 0, 0)]
        else:
            self.multi_account_line_ids = [(5, 0, 0)]        
    
    # =================================================
    # CREATE METHOD
    # =================================================
    @api.model
    def create(self, vals):
        # Auto-set journal if not provided and user has only one
        if 'journal_id' not in vals:
            user_journals = self.env.user.cash_treasury_journal_ids
            if len(user_journals) == 1:
                vals['journal_id'] = user_journals[0].id
            elif len(user_journals) == 0:
                raise UserError("You don't have permission to use any cash journal. Please contact administrator.")
        
        return super(CashTreasuryOut, self).create(vals)

    # =================================================
    # BUTTON: LOAD VENDOR BILLS
    # =================================================
    def action_load_vendor_bills(self):
        for rec in self:

            # ? NEW: no load inv with Multi Account 
            if rec.multi_account:
                raise UserError("You cannot load Vendor Bills in Multi Account mode.")

            if rec.pay_to_type != "partner":
                raise UserError("Load Vendor Bills is only allowed for Partner payments.")
            if not rec.partner_id:
                raise UserError("Please select a Partner first.")
            if rec.state != "draft":
                raise UserError("You can only load bills in Draft state.")

            bills = self.env["account.move"].search(
                [
                    ("move_type", "=", "in_invoice"),
                    ("state", "=", "posted"),
                    ("partner_id", "=", rec.partner_id.id),
                    ("amount_residual", ">", 0),
                ],
                order="invoice_date asc, id asc",
            )

            rec.allocation_line_ids = [(5, 0, 0)]

            lines = []
            for bill in bills:
                lines.append(
                    (
                        0,
                        0,
                        {
                            "invoice_id": bill.id,
                            "selected": False,
                            "amount_to_pay": 0.0,
                        },
                    )
                )

            rec.allocation_line_ids = lines
            rec.bills_loaded = True

    # =================================================
    # VALIDATIONS
    # =================================================
    
    @api.constrains("amount", "state")
    def _check_amount_positive(self):
        for rec in self:
            if rec.state != "draft" and (rec.amount is None or rec.amount <= 0):
                raise ValidationError(_("Amount must be greater than zero."))
                
                
    @api.constrains("state", "bills_loaded", "allocation_line_ids.amount_to_pay", "amount")
    def _check_diff_when_not_draft(self):
        for rec in self:
            if rec.state == "draft":
                continue
            if not rec.bills_loaded:
                continue

            total = sum(l.amount_to_pay or 0.0 for l in rec.allocation_line_ids)
            if float_compare(
                total,
                rec.amount or 0.0,
                precision_rounding=rec.currency_id.rounding,
            ) != 0:
                raise ValidationError(
                    "When vendor bills are loaded, total allocated must equal Cash Out amount."
                )

    @api.constrains("pay_to_type", "multi_account")
    def _check_multi_account_only_for_account(self):
        for rec in self:
            if rec.multi_account and rec.pay_to_type != "account":
                raise ValidationError(_("Multi Account is only allowed when Pay To Type is Account."))
                
    @api.constrains("multi_account", "partner_id")
    def _check_no_partner_in_multi_account(self):
        for rec in self:
            if rec.multi_account and rec.partner_id:
                raise ValidationError(_("Multi Account does not allow Partner."))
                
    @api.constrains("multi_account", "amount_manual")
    def _check_no_manual_amount_in_multi_account(self):
        for rec in self:
            if rec.multi_account and rec.amount_manual:
                raise ValidationError(_("Manual Amount is not allowed in Multi Account mode."))                

    # =================================================
    # WORKFLOW
    # =================================================
    def action_review(self):
        for rec in self:
            if rec.state != "draft":
                raise UserError("Only draft records can be reviewed.")
            rec.state = "reviewed"
            
    @api.depends(
        "multi_account",
        "multi_account_line_ids.amount",
        "amount_manual",
        "bills_loaded",
        "allocation_line_ids.selected",
        "allocation_line_ids.amount_to_pay",
    )
    def _compute_amount(self):
        for rec in self:
            if rec.multi_account:
                rec.amount = sum(rec.multi_account_line_ids.mapped("amount")) or 0.0

            elif rec.bills_loaded and rec.pay_to_type == "partner":
                selected_lines = rec.allocation_line_ids.filtered(
                    lambda l: l.selected and (l.amount_to_pay or 0.0) > 0
                )
                rec.amount = sum(selected_lines.mapped("amount_to_pay")) or 0.0

            else:
                rec.amount = rec.amount_manual or 0.0



    def _inverse_amount(self):
        for rec in self:
            if rec.multi_account:
                rec.amount = sum(rec.multi_account_line_ids.mapped("amount"))
            else:
                rec.amount_manual = rec.amount
                
    def action_approve(self):
        for rec in self:
            if rec.state != "reviewed":
                raise UserError("Only reviewed records can be approved.")
            # payment_date will be set later by Cash Entry
            rec.write({
                "state": "approved",
                "payment_date": False,
            })

    def action_back_to_draft(self):
        """
        Accountant: back to draft BEFORE paying (reviewed -> draft)
        """
        for rec in self:
            if rec.state != "reviewed":
                raise UserError("Only reviewed records can be reset to draft.")
            rec.write(
                {
                    "state": "draft",
                    "journal_entry_id": False,
                    "reversal_entry_id": False,
                    "name": False,
                    "payment_date": False,
                }
            )

    # =================================================
    # PAY (CREATE ENTRY + RECONCILE)
    # =================================================
    def action_pay(self):
        for rec in self:
            if rec.state != "approved":
                raise UserError("Only approved records can be paid.")
            if not rec.payment_date:
                raise UserError("Please set the Payment Date before paying.")

            # ---------- SEQUENCE (on payment) ----------
            seq_code = f"cash.out.{rec.journal_id.id}"
            sequence = self.env["ir.sequence"].search([("code", "=", seq_code)], limit=1)
            if not sequence:
                sequence = self.env["ir.sequence"].create(
                    {
                        "name": f"Cash Out {rec.journal_id.name}",
                        "code": seq_code,
                        "prefix": f"{rec.journal_id.code}/%(year)s-%(month)s/",
                        "padding": 4,
                        "company_id": rec.company_id.id,
                    }
                )

            seq_name = sequence.with_context(ir_sequence_date=rec.payment_date).next_by_id()

            credit_account = rec.journal_id.default_account_id
            if not credit_account:
                raise UserError("Journal has no default account.")

            lines = []

            # =====================================================
            # ? MULTI ACCOUNT MODE (NO PARTNER)
            # =====================================================
            if rec.multi_account:
                if rec.bills_loaded:
                    raise UserError("Multi Account cannot be used with Vendor Bills allocation.")

                if not rec.multi_account_line_ids:
                    raise UserError("Please add Multi Account lines first.")

                total = 0.0
                for l in rec.multi_account_line_ids:
                    if not l.account_id:
                        raise UserError("Each Multi Account line must have an Account.")
                    if not l.amount or l.amount <= 0:
                        raise UserError("Each Multi Account line Amount must be greater than zero.")

                    total += l.amount

                    lines.append(
                        (0, 0, {
                            "account_id": l.account_id.id,
                            "partner_id": False,   # ? NO PARTNER
                            "debit": l.amount,
                            "credit": 0.0,
                            "name": seq_name,
                        })
                    )

                # Credit line
                lines.append(
                    (0, 0, {
                        "account_id": credit_account.id,
                        "credit": total,
                        "debit": 0.0,
                        "name": seq_name,
                    })
                )

            # =====================================================
            # ? NORMAL MODE (ACCOUNT/PARTNER)
            # =====================================================
            else:
                debit_account = (
                    rec.partner_id.property_account_payable_id
                    if rec.pay_to_type == "partner"
                    else rec.account_id
                )
                if not debit_account:
                    raise UserError("Missing destination account.")

                # ---------- WITH BILLS ----------
                if rec.bills_loaded:
                    allocations = rec.allocation_line_ids.filtered(
                        lambda l: l.selected and l.amount_to_pay > 0
                    )
                    if not allocations:
                        raise UserError("Please select invoices and enter amounts.")

                    total = sum(al.amount_to_pay for al in allocations)

                    if float_compare(
                        total,
                        rec.amount or 0.0,
                        precision_rounding=rec.currency_id.rounding,
                    ) != 0:
                        raise UserError("Allocated total must equal Cash Out amount.")

                    for al in allocations:
                        lines.append(
                            (0, 0, {
                                "account_id": debit_account.id,
                                "partner_id": rec.partner_id.id,
                                "debit": al.amount_to_pay,
                                "credit": 0.0,
                                "name": seq_name,
                            })
                        )

                # ---------- WITHOUT BILLS ----------
                else:
                    total = rec.amount
                    lines.append(
                        (0, 0, {
                            "account_id": debit_account.id,
                            "partner_id": rec.partner_id.id if rec.pay_to_type == "partner" else False,
                            "debit": total,
                            "credit": 0.0,
                            "name": seq_name,
                        })
                    )

                # Credit line
                lines.append(
                    (0, 0, {
                        "account_id": credit_account.id,
                        "credit": total,
                        "debit": 0.0,
                        "name": seq_name,
                    })
                )

            # ---------- CREATE MOVE ----------
            move = self.env["account.move"].create(
                {
                    "move_type": "entry",
                    "journal_id": rec.journal_id.id,
                    "date": rec.payment_date,
                    "ref": seq_name,
                    "line_ids": lines,
                }
            )
            move.action_post()

            # ---------- RECONCILE (ONLY FOR BILLS) ----------
            if rec.bills_loaded and not rec.multi_account:
                debit_account = rec.partner_id.property_account_payable_id

                allocations = rec.allocation_line_ids.filtered(
                    lambda l: l.selected and (l.amount_to_pay or 0.0) > 0
                )

                
                pay_lines_dict = {}
                for pay_line in move.line_ids.filtered(
                    lambda l: l.account_id.id == debit_account.id
                    and not l.reconciled
                    and l.account_id.reconcile
                    and l.partner_id.id == rec.partner_id.id
                ):
                    amount_key = pay_line.debit
                    pay_lines_dict.setdefault(amount_key, []).append(pay_line)

                
                for al in allocations:
                    inv_lines = al.invoice_id.line_ids.filtered(
                        lambda l: l.account_id.id == debit_account.id
                        and not l.reconciled
                        and l.account_id.reconcile
                    )

                    if not inv_lines:
                        continue

                    pay_amount = al.amount_to_pay
                    
                    if pay_amount in pay_lines_dict and pay_lines_dict[pay_amount]:
                        pay_line = pay_lines_dict[pay_amount].pop(0)
                        (inv_lines + pay_line).reconcile()
                        
                        if not pay_lines_dict[pay_amount]:
                            del pay_lines_dict[pay_amount]
                    else:
                        raise UserError(
                            f"No matching payment line found for invoice {al.invoice_id.name} "
                            f"with amount {pay_amount}"
                        )
                    
                    

            rec.write({
                "name": seq_name,
                "journal_entry_id": move.id,
                "state": "paid",
            })


    # =================================================
    # SUPER APPROVER: CANCEL PAID -> DRAFT (REVERSAL ENTRY + UNRECONCILE)
    # =================================================
    def action_super_cancel_paid_to_draft(self):
        if not self.env.user.has_group("cash_treasury.group_cash_super_approver"):
            raise UserError("You are not allowed to perform this action.")

        not_paid = self.filtered(lambda r: r.state != "paid")
        if not_paid:
            raise UserError("All selected Cash Out records must be in Paid state.")

        today = fields.Date.context_today(self)

        for rec in self:
            move = rec.journal_entry_id
            if not move:
                raise UserError("No journal entry found on this document.")

            # 1) Unreconcile move lines
            for line in move.line_ids:
                if line.reconciled:
                    line.remove_move_reconcile()

            # 2) Create reversal move
            rev_lines = []
            for line in move.line_ids:
                vals = {
                    "account_id": line.account_id.id,
                    "partner_id": line.partner_id.id if line.partner_id else False,
                    "debit": line.credit,
                    "credit": line.debit,
                    "name": f"Reversal of {move.name or move.ref or rec.name}",
                }
                if line.currency_id:
                    vals["currency_id"] = line.currency_id.id
                    vals["amount_currency"] = -line.amount_currency
                rev_lines.append((0, 0, vals))

            rev_move = self.env["account.move"].create(
                {
                    "move_type": "entry",
                    "journal_id": move.journal_id.id,
                    "date": today,
                    "ref": f"Reversal of {move.ref or move.name or rec.name}",
                    "line_ids": rev_lines,
                }
            )
            rev_move.action_post()

            rec.reversal_entry_id = rev_move.id

            # 3) FIX: Auto-reconcile original move with reversal move
            # This prevents the original move from being reconciled again
            
            # Get all unreconciled lines from both moves
            move_lines = move.line_ids.filtered(lambda l: not l.reconciled and l.account_id.reconcile)
            rev_lines_list = rev_move.line_ids.filtered(lambda l: not l.reconciled and l.account_id.reconcile)
            
            # Group lines by account and partner
            lines_by_key = {}
            for line in move_lines:
                key = (line.account_id.id, line.partner_id.id or False)
                lines_by_key.setdefault(key, []).append(('original', line))
            
            for line in rev_lines_list:
                key = (line.account_id.id, line.partner_id.id or False)
                lines_by_key.setdefault(key, []).append(('reversal', line))
            
            # Reconcile matching lines
            for key, lines in lines_by_key.items():
                if len(lines) >= 2:
                    original_lines = [l for t, l in lines if t == 'original']
                    reversal_lines = [l for t, l in lines if t == 'reversal']
                    
                    if original_lines and reversal_lines:
                        # Try to reconcile lines with opposite balances
                        for o_line in original_lines:
                            for r_line in reversal_lines:
                                if not o_line.reconciled and not r_line.reconciled:
                                    if abs(o_line.balance + r_line.balance) < 0.01:
                                        try:
                                            (o_line + r_line).reconcile()
                                        except Exception:
                                            # If reconciliation fails, continue
                                            pass

            # 4) Return to draft
            rec.write(
                {
                    "state": "draft",
                    "journal_entry_id": False,
                    "name": False,
                    "payment_date": False,
                }
            )

        return True


# =====================================================
# ALLOCATION LINE
# =====================================================
class CashTreasuryOutAllocation(models.Model):
    _name = "cash.treasury.out.allocation"
    _description = "Cash Out Allocation Line"

    cash_out_id = fields.Many2one(
        "cash.treasury.out",
        required=True,
        ondelete="cascade",
    )

    selected = fields.Boolean(default=False)

    invoice_id = fields.Many2one(
        "account.move",
        domain="[('move_type','=','in_invoice'),('state','=','posted')]",
    )

    name = fields.Char(compute="_compute_invoice", store=True)

    residual_amount = fields.Monetary(
        compute="_compute_invoice",
        store=True,
        currency_field="currency_id",
    )

    amount_to_pay = fields.Monetary(currency_field="currency_id")

    currency_id = fields.Many2one(
        "res.currency",
        related="cash_out_id.currency_id",
        readonly=True,
    )

    @api.depends("invoice_id", "invoice_id.amount_residual", "invoice_id.name")
    def _compute_invoice(self):
        for rec in self:
            if rec.invoice_id:
                rec.name = rec.invoice_id.name
                rec.residual_amount = rec.invoice_id.amount_residual
            else:
                rec.name = False
                rec.residual_amount = 0.0

    @api.onchange("amount_to_pay")
    def _onchange_amount_to_pay(self):
        if not self.selected and self.amount_to_pay:
            self.amount_to_pay = 0.0
            return {
                "warning": {
                    "title": "Not Allowed",
                    "message": "You must select the invoice before entering an amount.",
                }
            }

        if (
            self.invoice_id
            and self.amount_to_pay
            and self.amount_to_pay > self.invoice_id.amount_residual
        ):
            self.amount_to_pay = self.invoice_id.amount_residual
            return {
                "warning": {
                    "title": "Invalid Amount",
                    "message": "Amount cannot exceed invoice residual.",
                }
            }

    @api.onchange("selected")
    def _onchange_selected(self):
        if not self.selected:
            self.amount_to_pay = 0.0
        elif self.invoice_id:
            self.amount_to_pay = self.invoice_id.amount_residual

