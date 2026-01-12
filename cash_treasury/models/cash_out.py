from odoo import models, fields, api, _
from odoo.exceptions import ValidationError, UserError
from odoo.tools.float_utils import float_compare


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

    partner_id = fields.Many2one("res.partner")
    account_id = fields.Many2one("account.account")

    amount = fields.Monetary(required=True)
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

    @api.depends("amount", "allocation_line_ids.amount_to_pay")
    def _compute_totals(self):
        for rec in self:
            total = sum(l.amount_to_pay or 0.0 for l in rec.allocation_line_ids)
            rec.total_allocated = total
            rec.allocation_diff = (rec.amount or 0.0) - total

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
    
    @api.constrains("amount")
    def _check_amount_positive(self):
        for rec in self:
            if rec.amount is None or rec.amount <= 0:
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

    # =================================================
    # WORKFLOW
    # =================================================
    def action_review(self):
        for rec in self:
            if rec.state != "draft":
                raise UserError("Only draft records can be reviewed.")
            rec.state = "reviewed"

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

            debit_account = (
                rec.partner_id.property_account_payable_id
                if rec.pay_to_type == "partner"
                else rec.account_id
            )
            if not debit_account:
                raise UserError("Missing destination account.")

            lines = []

            # ---------- WITH BILLS ----------
            if rec.bills_loaded:
                allocations = rec.allocation_line_ids.filtered(
                    lambda l: l.selected and l.amount_to_pay > 0
                )
                if not allocations:
                    raise UserError("Please select invoices and enter amounts.")

                total = sum(al.amount_to_pay for al in allocations)

                # ensure totals match (double safety)
                if float_compare(
                    total,
                    rec.amount or 0.0,
                    precision_rounding=rec.currency_id.rounding,
                ) != 0:
                    raise UserError("Allocated total must equal Cash Out amount.")

                for al in allocations:
                    lines.append(
                        (
                            0,
                            0,
                            {
                                "account_id": debit_account.id,
                                "partner_id": rec.partner_id.id,
                                "debit": al.amount_to_pay,
                                "credit": 0.0,
                                "name": seq_name,
                            },
                        )
                    )

            # ---------- WITHOUT BILLS ----------
            else:
                total = rec.amount
                lines.append(
                    (
                        0,
                        0,
                        {
                            "account_id": debit_account.id,
                            "partner_id": rec.partner_id.id if rec.pay_to_type == "partner" else False,
                            "debit": total,
                            "credit": 0.0,
                            "name": seq_name,
                        },
                    )
                )

            # ---------- CREDIT LINE ----------
            lines.append(
                (
                    0,
                    0,
                    {
                        "account_id": credit_account.id,
                        "credit": total,
                        "debit": 0.0,
                        "name": seq_name,
                    },
                )
            )

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

            # ---------- RECONCILE ----------
            if rec.bills_loaded:
                for al in rec.allocation_line_ids.filtered(
                    lambda l: l.selected and l.amount_to_pay > 0
                ):
                    inv_lines = al.invoice_id.line_ids.filtered(
                        lambda l: l.account_id.id == debit_account.id and not l.reconciled
                    )
                    pay_lines = move.line_ids.filtered(
                        lambda l: l.account_id.id == debit_account.id and not l.reconciled
                    )
                    (inv_lines + pay_lines).reconcile()

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

            # 1) Unreconcile move lines (so vendor bills residual returns)
            for line in move.line_ids:
                if line.reconciled:
                    line.remove_move_reconcile()

            # 2) Create reversal move (do NOT delete original move)
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

            # 3) Back to draft (clear number + payment date + link to entry)
            rec.write(
                {
                    "state": "draft",
                    "journal_entry_id": False,
                    "name": False,
                    "payment_date": False,
                }
            )

            rec.message_post(body="Paid entry reversed by Super Approver and document returned to Draft.")

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