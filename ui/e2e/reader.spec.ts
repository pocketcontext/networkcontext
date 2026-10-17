import { test, expect } from "@playwright/test";
const id = "record000000001";
const author = "profile00000001";
const READER_TITLE =
  "Reader test <script>window.evil=true</script> [bad](javascript:alert(1))";
test.beforeEach(async ({ page }) => {
  await page.route("**/api/collections/users/auth-with-password", (route) =>
    route.fulfill({
      json: {
        token:
          "test." +
          Buffer.from(JSON.stringify({ exp: 4102444800 })).toString(
            "base64url",
          ) +
          ".test",
        record: {
          id: "user00000000001",
          collectionName: "users",
          name: "Test",
        },
      },
    }),
  );
  await page.route("**/api/context/query", (route) => {
    const sql = route.request().postDataJSON().sql;
    let rows: any[] = [];
    if (sql.includes('FROM "posts"')) {
      if (sql.includes("OFFSET 30"))
        rows = [{ id: "record000000031", text: "Last result", posted_at: "2026-10-01 00:00:00.000Z" }];
      else if (sql.includes("instr(") && sql.includes("missing")) rows = [];
      else if (sql.includes("WHERE id="))
        rows = [
          {
            id,
            text: "Reader test <script>window.evil=true</script> [bad](javascript:alert(1))",
            author_profile: author,
            posted_at: "2026-10-14 08:30:00.000Z",
            url: "https://www.linkedin.com/feed/update/urn:li:activity:7516052678246400001/",
          },
        ];
      else
        rows = Array.from({ length: 31 }, (_, i) => ({
          id: i ? id.slice(0, 12) + String(i).padStart(3, "0") : id,
          text: "Reader test " + i,
          posted_at: "2026-10-14 08:30:00.000Z",
        }));
    }
    if (sql.includes('FROM "profiles"') && sql.includes("WHERE id="))
      rows = [{ id: author, name: "Ada Example", headline: "Head of Data" }];
    const columns = rows.length ? Object.keys(rows[0]) : [];
    return route.fulfill({
      json: {
        columns,
        rows: rows.map((r) => columns.map((c) => r[c])),
        truncated: false,
      },
    });
  });
});
test("deep link survives sign in, related labels resolve and markdown stays inert", async ({
  page,
}) => {
  await page.goto("/#/posts/" + id);
  await page.getByLabel("Email", { exact: true }).fill("test@example.com");
  await page.getByLabel("Password", { exact: true }).fill("test-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: READER_TITLE, exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Ada Example", exact: true }),
  ).toBeVisible();
  expect(await page.evaluate(() => Boolean((window as any).evil))).toBe(false);
  await expect(page.locator('a[href^="javascript:"]')).toHaveCount(0);
  await page.reload();
  await expect(
    page.getByRole("heading", { name: READER_TITLE, exact: true }),
  ).toBeVisible();
  await page
    .getByRole("link", { name: "Ada Example", exact: true })
    .click();
  await expect(page).toHaveURL(new RegExp("/profiles/" + author));
  await page.goBack();
  await expect(page.getByRole("heading", {name:READER_TITLE,exact:true})).toBeVisible();
});
test("search is server-paginated and mobile browse is usable", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/#/posts");
  await page.getByLabel("Email", { exact: true }).fill("test@example.com");
  await page.getByLabel("Password", { exact: true }).fill("test-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await page.getByRole("button", { name: "Browse", exact: true }).click();
  await page.getByRole("button", { name: "Next", exact: true }).click();
  await expect(page.getByText("Last result")).toBeVisible();
  await page.getByRole("button", {name:"Previous",exact:true}).click();
  await page.getByLabel("Search posts").fill("missing");
  await expect(page.getByText("No matching records.")).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});
