import requests
import logging
from django.conf import settings
from .models import NotificationLog, NotificationTemplate

logger = logging.getLogger(__name__)


class SMSService:
    """
    Service to handle sending SMS notifications via 3rd party gateways
    (Arkesel, Hubtel, Twilio, etc.)
    """

    @staticmethod
    def send_receipt(sale):
        """
        Sends a digital receipt to the customer attached to the sale.
        """
        if not sale.customer or not sale.customer.phone_number:
            return False, "No customer or phone number linked to sale."

        # 1. Prepare Message
        # Try to find a template, else use default
        try:
            template = NotificationTemplate.objects.get(type='SMS', name='Receipt', is_active=True)
            message = template.content.format(
                name=sale.customer.first_name,
                invoice=sale.invoice_number,
                amount=sale.total_amount,
                date=sale.created_at.strftime('%d/%m/%Y')
            )
        except NotificationTemplate.DoesNotExist:
            # Default Fallback Message
            shop_name = sale.location.name
            message = f"Thanks for shopping at {shop_name}. Inv: {sale.invoice_number}. Total: {sale.total_amount}. Date: {sale.created_at.strftime('%d/%m/%Y')}."

        # 2. Send SMS
        return SMSService.send_sms(
            recipient=sale.customer.phone_number,
            message=message,
            customer=sale.customer,
            sale=sale
        )

    @staticmethod
    def send_sms(recipient, message, customer=None, sale=None):
        """
        Generic sender method.
        Currently configured for Arkesel (popular in Ghana) as an example structure.
        """
        api_key = getattr(settings, 'SMS_API_KEY', None)
        sender_id = getattr(settings, 'SMS_SENDER_ID', 'RetailPOS')

        # Create Log Entry (Pending)
        log = NotificationLog.objects.create(
            customer=customer,
            sale=sale,
            type='SMS',
            recipient=recipient,
            message_content=message,
            status=NotificationLog.Status.QUEUED
        )

        if not api_key:
            # Simulation Mode for Development
            print(f"--- [SIMULATION SMS] To: {recipient} | Msg: {message} ---")
            log.status = NotificationLog.Status.SENT
            log.gateway_response = {"info": "Simulation Mode - API Key missing"}
            log.save()
            return True, "Simulated sent"

        # Actual API Request (Example for Arkesel)
        url = "https://sms.arkesel.com/api/v2/sms/send"
        payload = {
            "sender": sender_id,
            "message": message,
            "recipients": [recipient]
        }
        headers = {"api-key": api_key}

        try:
            response = requests.post(url, json=payload, headers=headers)
            response_data = response.json()

            if response.status_code == 200 and response_data.get('status') == 'success':
                log.status = NotificationLog.Status.SENT
                log.gateway_response = response_data
                log.save()
                return True, "Sent successfully"
            else:
                log.status = NotificationLog.Status.FAILED
                log.error_message = str(response_data)
                log.save()
                return False, f"Gateway Error: {response_data}"

        except Exception as e:
            log.status = NotificationLog.Status.FAILED
            log.error_message = str(e)
            log.save()
            return False, str(e)