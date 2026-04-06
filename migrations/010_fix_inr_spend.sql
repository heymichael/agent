-- Fix bug #158: Shipra Sharma and Anubhav Film invoices were synced from
-- Bill.com in INR without currency conversion.  Apply 1 USD = 100 INR.
UPDATE vendor_monthly_spend
SET    total_amount = round(total_amount / 100, 2),
       synced_at    = now()
WHERE  vendor_id IN (
         '299e77a9-3e15-4b62-9f7f-7dbd239c2c73',
         'a58595fa-1857-4ef1-89e7-0f579ca0a3b9'
       );
