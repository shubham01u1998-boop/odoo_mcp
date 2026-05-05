"""One-shot script: re-save all Tiffin Connect ticket descriptions as HTML.
Run: venv/Scripts/python.exe migrate_descriptions.py
"""
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

import markdown as _md
import xmlrpc.client

ODOO_URL = os.environ["ODOO_URL"].rstrip("/")
ODOO_DB  = os.environ["ODOO_DB"]
ODOO_USER = os.environ["ODOO_USERNAME"]
ODOO_KEY  = os.environ["ODOO_API_KEY"]
PROJECT_ID = 58  # Tiffin Connect

DESCRIPTIONS = {
    2315: """## Description
Implement OTP-based authentication using Firebase and backend verification.

## Tasks
1. Integrate Firebase Admin SDK
2. Verify ID token
3. Create user if not exists
4. Store role
5. Build auth middleware

## APIs
- POST /auth/verify

## Acceptance Criteria
- User logs in via OTP
- User created on first login
- Unauthorized access blocked""",

    2316: """## Description
Mobile OTP login screen with Firebase integration and session persistence.

## Tasks
1. Build login UI
2. Integrate Firebase
3. Store session

## Acceptance Criteria
- OTP login works
- Session persists""",

    2317: """## Description
Profile and address CRUD APIs with default address and snapshot logic.

## Tasks
1. Create profile APIs
2. Address CRUD APIs
3. Implement default address logic
4. Store snapshot in orders/subscriptions

## APIs
- POST /user/profile
- GET /user/profile
- POST /addresses
- GET /addresses
- PUT /addresses/{id}
- DELETE /addresses/{id}

## Acceptance Criteria
- Multiple addresses supported
- Default address works correctly
- Snapshot stored on order/subscription creation""",

    2318: """## Description
Profile setup form and address management UI with default toggle.

## Tasks
1. Profile form
2. Address UI
3. Default address toggle

## Acceptance Criteria
- Profile setup complete
- Address CRUD works""",

    2319: """## Description
KYC submission, document upload via Azure Blob Storage, status tracking and rejection flow.

## Tasks
1. KYC schema design
2. Azure upload integration
3. Status handling (Pending / Approved / Rejected)
4. Rejection and resubmission flow

## APIs
- POST /vendor/kyc
- GET /vendor/kyc/status
- PUT /vendor/kyc/resubmit

## Acceptance Criteria
- KYC submitted successfully
- Status tracked correctly
- Re-upload works after rejection""",

    2320: """## Description
KYC form, document upload UI and status screen for vendors.

## Tasks
1. KYC form
2. Document upload UI
3. Status screen

## Acceptance Criteria
- Vendor can submit KYC
- Status is visible after submission""",

    2321: """## Description
Vendor listing API with pincode filtering and sorting by distance and rating.

## Tasks
1. Fetch vendors by pincode
2. Implement sorting (distance, rating)

## APIs
- GET /vendors

## Acceptance Criteria
- Vendors filtered correctly by pincode
- Sorted list returned""",

    2322: """## Description
Vendor list screen with filters and address display.

## Tasks
1. Vendor list UI
2. Filter UI
3. Address display

## Acceptance Criteria
- Vendors visible on home screen
- Filters work correctly""",

    2323: """## Description
Full order creation flow with capacity check, approval logic and state machine.

## State Machine
CREATED → PENDING_APPROVAL → APPROVED → PAYMENT_PENDING → PAID → DELIVERED → CANCELLED → EXPIRED

## Tasks
1. Create order entity
2. Store address snapshot
3. Check vendor capacity limit
4. Auto approve or mark pending based on capacity
5. Implement full state machine
6. Auto expire unpaid orders

## APIs
- POST /orders
- GET /orders/{id}
- GET /orders

## Acceptance Criteria
- Order created successfully
- Capacity logic applied correctly
- Correct state transitions occur
- Expired orders handled automatically""",

    2324: """## Description
Cart screen, order summary and place order flow.

## Tasks
1. Cart screen
2. Order summary
3. Place order action

## Acceptance Criteria
- Order flow works end-to-end
- Summary is accurate""",

    2325: """## Description
Mock payment flow that simulates success/failure and updates order payment status.

## Tasks
1. Payment status update logic
2. Simulate success/failure responses

## APIs
- POST /payments/init

## Acceptance Criteria
- Payment flow simulated correctly
- Order status updated after payment""",

    2326: """## Description
Payment screen with success and failure state UI.

## Tasks
1. Payment screen
2. Success state UI
3. Failure state UI

## Acceptance Criteria
- Both payment states visible and handled correctly""",

    2327: """## Description
Vendor-facing daily order list with approve/reject and mark delivered APIs.

## Tasks
1. Fetch daily orders for vendor
2. Approve/reject order logic
3. Mark order as delivered

## APIs
- GET /vendor/orders
- POST /vendor/orders/{id}/approve
- POST /vendor/orders/{id}/deliver

## Acceptance Criteria
- Vendor sees their daily orders
- Can approve or reject orders
- Can mark orders as delivered""",

    2328: """## Description
Vendor dashboard with order list and action buttons.

## Tasks
1. Dashboard screen
2. Order list
3. Approve/reject/deliver action buttons

## Acceptance Criteria
- Orders visible on dashboard
- Actions work correctly""",

    2329: """## Description
Weekly menu CRUD with time slot configuration and order cutoff validation.

## Tasks
1. Menu CRUD APIs
2. Slot configuration
3. Cutoff time validation

## APIs
- POST /vendor/menu
- GET /vendor/menu
- PUT /vendor/menu

## Acceptance Criteria
- Menu can be created and updated
- Cutoff time is enforced""",

    2330: """## Description
Weekly planner interface with slot selection for vendors.

## Tasks
1. Weekly planner UI
2. Time slot selection

## Acceptance Criteria
- Vendor can configure weekly menu""",

    2331: """## Description
Subscription creation with daily meal generation, capacity logic, and price snapshot.

## Tasks
1. Create subscription entity
2. Generate daily meals for subscription period
3. Store price snapshot at time of subscription

## APIs
- POST /subscriptions
- GET /subscriptions

## Acceptance Criteria
- Subscription created successfully
- Daily meals generated correctly
- Pricing locked at subscription time""",

    2332: """## Description
Plan selection and subscribe flow UI.

## Tasks
1. Plan selection screen
2. Subscribe flow

## Acceptance Criteria
- Subscription flow works end-to-end""",

    2333: """## Description
Calendar planner data with skip meal API and skip limit validation.

## Tasks
1. Fetch planner calendar data
2. Skip meal API
3. Validate skip limits

## APIs
- GET /planner
- POST /planner/skip

## Acceptance Criteria
- Skip functionality works
- Skip limits are enforced""",

    2334: """## Description
Calendar UI showing meals for the subscription period with skip action.

## Tasks
1. Calendar UI
2. Skip meal action

## Acceptance Criteria
- Meals visible on calendar
- Skip action works""",

    2335: """## Description
Admin panel APIs — KYC approval/rejection, order monitoring and coupon CRUD.

## Tasks
1. KYC approve/reject APIs
2. Order monitoring list
3. Coupon CRUD

## APIs
- POST /admin/kyc/approve
- GET /admin/orders
- POST /admin/coupons

## Acceptance Criteria
- All admin controls working correctly""",

    2336: """## Description
Admin dashboard with KYC management screen and orders screen.

## Tasks
1. Admin dashboard
2. KYC review screen
3. Orders monitoring screen

## Acceptance Criteria
- Admin can view and manage the full system""",

    2337: """## Description
Support ticket and rating submission APIs.

## Tasks
1. Support ticket APIs
2. Rating submission APIs

## APIs
- POST /tickets
- POST /ratings

## Acceptance Criteria
- Support tickets created successfully
- Ratings stored correctly""",

    2338: """## Description
Support ticket submission form and rating UI.

## Tasks
1. Ticket submission form
2. Rating UI

## Acceptance Criteria
- User can submit a support ticket
- User can submit a rating""",

    2339: """## Description
Store notifications and trigger notification events for key system actions.

## Tasks
1. Store notifications in DB
2. Trigger notification events

## APIs
- GET /notifications

## Acceptance Criteria
- Notifications saved correctly
- Events trigger notifications as expected""",

    2340: """## Description
Notification list screen with read/unread state management.

## Tasks
1. Notification list UI
2. Read/unread state

## Acceptance Criteria
- Notifications visible in list
- Read/unread state works correctly""",
}


def md_to_html(text: str) -> str:
    return _md.markdown(text, extensions=["nl2br", "sane_lists"])


def main():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_KEY, {})
    if not uid:
        print("AUTH FAILED"); return

    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

    ok = 0
    for ticket_id, md_text in DESCRIPTIONS.items():
        html_body = md_to_html(md_text)
        result = models.execute_kw(
            ODOO_DB, uid, ODOO_KEY,
            "project.task", "write",
            [[ticket_id], {"description": html_body}],
        )
        status = "OK" if result else "FAIL"
        print(f"  {status}  ticket {ticket_id}")
        if result:
            ok += 1

    print(f"\nDone: {ok}/{len(DESCRIPTIONS)} updated")


if __name__ == "__main__":
    main()
