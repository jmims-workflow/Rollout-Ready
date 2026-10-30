// Captures the README screenshots and checks for sideways scrolling.
// Start the app first (rollout serve), then: node scripts/screenshots.js
// Needs Playwright for Node (npm install playwright) or a preinstalled copy.
const { chromium } = require("playwright");
const path = require("path");

const BASE = process.env.BASE_URL || "http://127.0.0.1:8080";
const OUT = path.join(__dirname, "..", "docs", "screenshots");
const EXAMPLE = "/prompts/followup-email/examples/ridgeway-pricing?current=v2&new=v3";

const shots = [
  { name: "start-desktop.png", url: "/", width: 1280, height: 900 },
  { name: "start-phone.png", url: "/", width: 390, height: 844, mobile: true },
  { name: "results-desktop.png", url: "/results", width: 1280, height: 900 },
  // Open the disagree form so the screenshot shows it.
  { name: "example-detail-desktop.png", url: EXAMPLE, width: 1280, height: 900, open: "details.disagree" },
  { name: "test-a-change-desktop.png", url: "/test", width: 1280, height: 900 },
  { name: "new-prompt-desktop.png", url: "/prompts/new", width: 1280, height: 900 },
];

const narrowPages = ["/", "/results", "/results?tab=good", "/results?tab=changes", "/results?tab=details",
  "/results?suite=webinar-invite&current=v2&new=v3", "/results?suite=partner-outreach",
  EXAMPLE, "/test", "/prompts", "/prompts/followup-email", "/prompts/new", "/how-it-works"];

(async () => {
  const browser = await chromium.launch();
  for (const s of shots) {
    const ctx = await browser.newContext({ viewport: { width: s.width, height: s.height }, deviceScaleFactor: s.mobile ? 2 : 1, isMobile: !!s.mobile, hasTouch: !!s.mobile });
    const page = await ctx.newPage();
    await page.goto(BASE + s.url, { waitUntil: "networkidle" });
    if (s.open) await page.evaluate((sel) => document.querySelectorAll(sel).forEach((d) => { d.open = true; }), s.open);
    await page.screenshot({ path: path.join(OUT, s.name), fullPage: true });
    console.log("saved", s.name);
    await ctx.close();
  }
  let problems = 0;
  const ctx = await browser.newContext({ viewport: { width: 320, height: 700 } });
  const page = await ctx.newPage();
  for (const url of narrowPages) {
    await page.goto(BASE + url, { waitUntil: "networkidle" });
    await page.evaluate(() => document.querySelectorAll("details").forEach((d) => { d.open = true; }));
    const over = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    if (over > 0) { problems++; console.log("sideways scroll at 320px:", url, over + "px"); }
  }
  await browser.close();
  console.log(problems ? problems + " page(s) scroll sideways at 320px" : "No page scrolls sideways at 320px");
  process.exit(problems ? 1 : 0);
})();
