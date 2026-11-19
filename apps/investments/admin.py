from django.contrib import admin
from .models import InvestmentPlan, Deposit, Investment, Transaction
from .services import DepositNotPending, confirm_deposit, reject_deposit


@admin.register(InvestmentPlan)
class InvestmentPlanAdmin(admin.ModelAdmin):
    list_display = ['name', 'roi_percentage', 'duration', 'duration_unit', 'min_amount', 'max_amount', 'is_active', 'is_featured']
    list_filter = ['is_active', 'is_featured', 'duration_unit']
    search_fields = ['name']


@admin.register(Deposit)
class DepositAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'amount', 'payment_method', 'status', 'created_at']
    list_filter = ['status', 'payment_method']
    search_fields = ['user__email', 'transaction_hash']
    actions = ['approve_deposits', 'reject_deposits']
    # Status can only change through the actions below. Editing the status field by hand
    # marked deposits "confirmed" WITHOUT crediting the wallet.
    readonly_fields = ['status', 'confirmed_at']

    @admin.action(description='Approve selected deposits (credits wallet)')
    def approve_deposits(self, request, queryset):
        done = skipped = 0
        for deposit_id in list(queryset.filter(status='pending').values_list('id', flat=True)):
            try:
                confirm_deposit(deposit_id)
                done += 1
            except DepositNotPending:
                skipped += 1
        self.message_user(request, f'{done} deposit(s) approved and credited to wallets. {skipped} skipped.')

    @admin.action(description='Reject selected deposits')
    def reject_deposits(self, request, queryset):
        done = skipped = 0
        for deposit_id in list(queryset.filter(status='pending').values_list('id', flat=True)):
            try:
                reject_deposit(deposit_id)
                done += 1
            except DepositNotPending:
                skipped += 1
        self.message_user(request, f'{done} deposit(s) rejected. {skipped} skipped.')


@admin.register(Investment)
class InvestmentAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'plan', 'amount', 'expected_return', 'status', 'ends_at']
    list_filter = ['status', 'plan']
    search_fields = ['user__email']


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'type', 'amount', 'status', 'created_at']
    list_filter = ['type', 'status']
    search_fields = ['user__email', 'reference']