// Captures the README screenshots and checks for sideways scrolling.
// Start the app first (rollout serve), then: node scripts/screenshots.js
// Needs Playwright for Node (npm install playwright) or a preinstalled copy.
const { chromium } = require("playwright");
const path = require("path");

const BASE = process.env.BASE_URL || "http://127.0.0.1:8080";
const OUT = path.join(__dirname, "..", "docs", "screenshots");

const shots = [
  { name: "compare-desktop.png", url: "/", width: 1280, height: 900, full: true },
  { name: "case-detail-desktop.png", url: "/suites/followup-email/cases/ridgeway-pricing?current=v2&new=v3", width: 1280, height: 900, full: true },
  { name: "test-new-version-desktop.png", url: "/test", width: 1280, height: 900, full: true },
  { name: "new-suite-desktop.png", url: "/suites/new", width: 1280, height: 900, full: true },
  { name: "compare-phone.png", url: "/", width: 390, height: 844, full: true, mobile: true },
];

const narrowPages = ["/", "/?tab=rubric", "/?tab=changes", "/?tab=details",
  "/suites/followup-email/cases/ridgeway-pricing?current=v2&new=v3", "/test", "/suites",
  "/suites/followup-email", "/suites/new", "/scoring"];

(async () => {
  const browser = await chromium.launch();
  for (const s of shots) {
    const ctx = await browser.newContext({ viewport: { width: s.width, height: s.height }, deviceScaleFactor: s.mobile ? 2 : 1, isMobile: !!s.mobile, hasTouch: !!s.mobile });
    const page = await ctx.newPage();
    await page.goto(BASE + s.url, { waitUntil: "networkidle" });
    await page.screenshot({ path: path.join(OUT, s.name), fullPage: s.full });
    console.log("saved", s.name);
    await ctx.close();
  }
  let problems = 0;
  const ctx = await browser.newContext({ viewport: { width: 320, height: 700 } });
  const page = await ctx.newPage();
  for (const url of narrowPages) {
    await page.goto(BASE + url, { waitUntil: "networkidle" });
    const over = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    if (over > 0) { problems++; console.log("sideways scroll at 320px:", url, over + "px"); }
  }
  await browser.close();
  console.log(problems ? problems + " page(s) scroll sideways at 320px" : "No page scrolls sideways at 320px");
  process.exit(problems ? 1 : 0);
})();
