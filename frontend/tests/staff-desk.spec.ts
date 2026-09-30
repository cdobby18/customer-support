import { test, expect } from '@playwright/test';

const API_URL = process.env.API_URL ?? 'http://localhost:8000';
const ADMIN_EMAIL = process.env.E2E_ADMIN_EMAIL ?? 'e2e-admin@relay.dev';
const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD ?? 'e2e-admin-password';
const CUSTOMER_PASSWORD = 'correct horse battery';
// Unique per run: earlier runs leave their own ticket in the queue, and a
// shared message would make the row locator match the wrong conversation.
const RUN_ID = Date.now().toString(36);
const TICKET_MESSAGE = `Staff desk e2e ${RUN_ID} - refund not received`;
// A public comment kicks off server-side auto-response work, so the first
// submit of a run can be slow while models warm up.
const SUBMIT_TIMEOUT = 20_000;

async function postJson(path: string, body: unknown, token?: string) {
  const response = await fetch(`${API_URL}${path}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`${path} failed: ${response.status} ${text}`);
  }
  return response.json();
}

async function registerAndLogin(email: string, password: string) {
  // A pre-existing email is fine here: these specs re-login the same user.
  await fetch(`${API_URL}/auth/register`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  });
  return postJson('/auth/login', { email, password });
}

async function loginAs(page: import('@playwright/test').Page, email: string, password: string) {
  await page.goto('/');
  await page.fill('input[type="email"]', email);
  await page.fill('input[type="password"]', password);
  await page.click('button:has-text("Open support desk")');
}

async function signOut(page: import('@playwright/test').Page) {
  await page.click('button[title="Sign out"]');
  await expect(page.locator('text=Welcome back')).toBeVisible();
}

test.describe('Staff desk', () => {
  // The four checks below share one ticket and build on each other, so they
  // must not be reordered or run concurrently.
  test.describe.configure({ mode: 'serial' });

  let ticketId: string;
  let customerEmail: string;

  test.beforeAll(async () => {
    customerEmail = `staff-desk-${Date.now()}@example.com`;
    const customer = await registerAndLogin(customerEmail, CUSTOMER_PASSWORD);
    const ticket = await postJson(
      '/tickets',
      { customer_id: customer.user.id, message: TICKET_MESSAGE, channel: 'web' },
      customer.access_token,
    );
    ticketId = ticket.id;

    const form = new FormData();
    form.append('files', new Blob(['order 1234 receipt'], { type: 'text/plain' }), 'receipt.txt');
    const uploaded = await fetch(`${API_URL}/tickets/${ticketId}/attachments`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${customer.access_token}` },
      body: form,
    });
    if (!uploaded.ok) throw new Error(`attachment upload failed: ${uploaded.status}`);
  });

  test('Staff sees the customer ticket with its attachment', async ({ page }) => {
    await loginAs(page, ADMIN_EMAIL, ADMIN_PASSWORD);
    await expect(page.locator('text=Support queue')).toBeVisible();

    const row = page.locator(`text=${TICKET_MESSAGE}`).first();
    await expect(row).toBeVisible();
    await row.click();

    await expect(page.locator('h3:has-text("Conversation")')).toBeVisible();
    await expect(page.locator('h3:has-text("Attachments")')).toBeVisible();
    await expect(page.locator('.attachment-name')).toHaveText('receipt.txt');
  });

  test('Staff writes a public reply and an internal note', async ({ page }) => {
    await loginAs(page, ADMIN_EMAIL, ADMIN_PASSWORD);
    await page.locator(`text=${TICKET_MESSAGE}`).first().click();

    // Scoped to the form: the submit button's label flips between "Send" and
    // "Send reply" depending on the visibility toggle.
    const form = page.locator('.comment-form');
    const sendButton = form.locator('button.primary-button');
    // Type rather than fill(): fill() only writes the DOM value, so React
    // re-renders can wipe it. The delay gives React a chance to commit each
    // character, otherwise a re-render lands mid-word and drops the tail.
    const draft = (text: string) => form.locator('textarea').pressSequentially(text, { delay: 25 });

    await page.click('button:has-text("Reply to customer")');
    await draft('We have issued the refund.');
    await sendButton.click();
    await expect(page.locator('text=We have issued the refund.')).toBeVisible({ timeout: SUBMIT_TIMEOUT });

    await page.click('button:has-text("Internal note")');
    await draft('Ask billing to confirm the ledger entry.');
    await sendButton.click();
    await expect(page.locator('text=Ask billing to confirm the ledger entry.')).toBeVisible({ timeout: SUBMIT_TIMEOUT });
  });

  test('Customer sees the public reply but never the internal note', async ({ page }) => {
    await loginAs(page, customerEmail, CUSTOMER_PASSWORD);
    const row = page.locator(`text=${TICKET_MESSAGE}`).first();
    await expect(row).toBeVisible();
    await row.click();

    await expect(page.locator('text=We have issued the refund.')).toBeVisible({ timeout: SUBMIT_TIMEOUT });
    await expect(page.locator('text=Ask billing to confirm the ledger entry.')).toHaveCount(0);
  });

  test('Staff resolves the ticket and the customer sees the new status', async ({ page }) => {
    await loginAs(page, ADMIN_EMAIL, ADMIN_PASSWORD);
    await page.locator(`text=${TICKET_MESSAGE}`).first().click();
    await page.click('.status-actions button:has-text("resolved")');
    await expect(page.locator('.status-actions button.active')).toHaveText('resolved');

    await signOut(page);
    await loginAs(page, customerEmail, CUSTOMER_PASSWORD);
    await page.locator(`text=${TICKET_MESSAGE}`).first().click();
    await expect(page.locator('.detail-meta strong').first()).toHaveText('resolved');
  });
});
