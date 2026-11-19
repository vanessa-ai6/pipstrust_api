from django.contrib import admin
from .models import Withdrawal
from .services import WithdrawalNotActionable, complete_withdrawal, reject_withdrawal


@admin.register(Withdrawal)
class WithdrawalAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'amount', 'method', 'status', 'created_at']
    list_filter = ['status', 'method']
    search_fields = ['user__email']
    actions = ['mark_completed', 'mark_processing', 'reject_withdrawals']
    # Status changes must go through the actions: they move money and send the emails.
    readonly_fields = ['status', 'processed_at']

    @admin.action(description='Mark as Completed')
    def mark_completed(self, request, queryset):
        done = skipped = 0
        for wid in list(queryset.filter(status__in=['otp_verified', 'processing']).values_list('id', flat=True)):
            try:
                complete_withdrawal(wid)
                done += 1
            except WithdrawalNotActionable:
                skipped += 1
        self.message_user(request, f'{done} withdrawal(s) marked as completed. {skipped} skipped.')

    @admin.action(description='Mark as Processing')
    def mark_processing(self, request, queryset):
        updated = queryset.filter(status='otp_verified').update(status='processing')
        self.message_user(request, f'{updated} withdrawal(s) marked as processing.')

    @admin.action(description='Reject & Refund')
    def reject_withdrawals(self, request, queryset):
        done = skipped = 0
        for wid in list(queryset.filter(status__in=['pending', 'otp_verified', 'processing']).values_list('id', flat=True)):
            try:
                reject_withdrawal(wid)
                done += 1
            except WithdrawalNotActionable:
                skipped += 1
        self.message_user(request, f'{done} withdrawal(s) rejected and refunded. {skipped} skipped.')
