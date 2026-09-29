import { test, expect } from '@playwright/test';

const API_URL = process.env.API_URL ?? 'http://localhost:8000';

async function createUserViaAPI(email: string, password: string) {
  const response = await fetch(`${API_URL}/auth/register`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`Registration failed: ${response.status} ${text}`);
  }
  return response.json();
}

async function loginViaAPI(email: string, password: string) {
  const response = await fetch(`${API_URL}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`Login failed: ${response.status} ${text}`);
  }
  return response.json();
}

test.describe('Auth + Ticket Lifecycle', () => {
  let testEmail: string;
  let testPassword: string;
  let accessToken: string;
  let userId: string;
  let ticketId: string;

  test.beforeAll(async () => {
    testEmail = `test-${Date.now()}@example.com`;
    testPassword = 'correct horse battery';
    await createUserViaAPI(testEmail, testPassword);
    const loginResult = await loginViaAPI(testEmail, testPassword);
    accessToken = loginResult.access_token;
    userId = loginResult.user.id;
  });

  test('Register → Login → Create Ticket → View Ticket → Add Comment → Logout', async ({ page }) => {
    // 1. Login via UI
    await page.goto('/');
    await page.fill('input[type="email"]', testEmail);
    await page.fill('input[type="password"]', testPassword);
    await page.click('button:has-text("Open support desk")');
    // Customer sees "Your conversations", not "Support queue"
    await expect(page.locator('text=Your conversations')).toBeVisible();

    // 2. Create a new ticket
    await page.fill('textarea[placeholder="Tell us what happened..."]', 'E2E test ticket - need help with login');
    await page.click('button:has-text("Submit ticket")');
    await expect(page.locator('text=Ticket created')).toBeVisible();

    // 3. Verify ticket appears in list
    const ticketRow = page.locator('text=E2E test ticket - need help with login').first();
    await expect(ticketRow).toBeVisible();

    // Click the ticket to view details
    await ticketRow.click();
    await expect(page.locator('h3:has-text("Conversation")')).toBeVisible();
    await expect(page.locator('h2:has-text("E2E test ticket - need help with login")')).toBeVisible();

    // 4. Add a comment
    await page.fill('textarea[placeholder="Write a reply..."]', 'This is a test comment from E2E test');
    await page.click('button:has-text("Send")');
    await expect(page.locator('text=This is a test comment from E2E test')).toBeVisible();

    // 5. Logout
    const uiToken = JSON.parse(
      await page.evaluate(() => localStorage.getItem('relay-session') ?? '{}')
    ).access_token;
    await page.click('button[title="Sign out"]');
    await expect(page.locator('text=Welcome back')).toBeVisible();

    // 6. Signing out revokes the token server-side, not just locally
    const revokedCheck = await fetch(`${API_URL}/auth/me`, {
      headers: { 'Authorization': `Bearer ${uiToken}` },
    });
    expect(revokedCheck.status).toBe(401);
  });

  test('Customer cannot access other customer tickets', async ({ page }) => {
    // Create second user
    const email2 = `test2-${Date.now()}@example.com`;
    await createUserViaAPI(email2, testPassword);
    const login2 = await loginViaAPI(email2, testPassword);

    // Create ticket as first user via API
    const ticketResponse = await fetch(`${API_URL}/tickets`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${accessToken}`,
      },
      body: JSON.stringify({
        customer_id: userId,
        message: 'Private ticket for user 1',
        channel: 'web',
      }),
    });
    if (!ticketResponse.ok) {
      const text = await ticketResponse.text();
      throw new Error(`Ticket creation failed: ${ticketResponse.status} ${text}`);
    }
    const ticket = await ticketResponse.json();
    ticketId = ticket.id;

    // Login as second user
    await page.goto('/');
    await page.fill('input[type="email"]', email2);
    await page.fill('input[type="password"]', testPassword);
    await page.click('button:has-text("Open support desk")');

    // Verify the other user's ticket is NOT visible in the list
    await expect(page.locator('text=Private ticket for user 1')).not.toBeVisible();

    // Verify API directly returns 403 when accessing the ticket
    const apiResponse = await fetch(`${API_URL}/tickets/${ticketId}`, {
      headers: { 'Authorization': `Bearer ${login2.access_token}` },
    });
    expect(apiResponse.status).toBe(403);
  });
});