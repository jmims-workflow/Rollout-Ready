// Captures the README screenshots and checks for sideways scrolling.
// Start the app first (rollout serve), then: node scripts/screenshots.js
// Needs Playwright for Node (npm install playwright) or a preinstalled copy.
const { chromium } = require("playwright");
const path = require("path");

const BASE = process.env.BASE_URL || "http://127.0.0.1:8080";
const OUT = path.join(__dirname, "..", "docs", "screenshots");
const EXAMPLE = "/prompts/followup-email/examples/ridgeway-pricing?current=v2&new=v3";
const NOT_READY = "/i/followup-email/result?current=v2&new=v3";
const READY = "/i/followup-email/result?current=v2&new=v4";

const shots = [
  { name: "start-desktop.png", url: "/", width: 1280, height: 900 },
  { name: "start-phone.png", url: "/", width: 390, height: 844, mobile: true },
  { name: "result-not-ready-desktop.png", url: NOT_READY, width: 1280, height: 900 },
  { name: "result-not-ready-phone.png", url: NOT_READY, width: 390, height: 844, mobile: true },
  { name: "result-ready-desktop.png", url: READY, width: 1280, height: 900 },
  { name: "result-ready-phone.png", url: READY, width: 390, height: 844, mobile: true },
  { name: "instructions-desktop.png", url: "/i/followup-email", width: 1280, height: 900 },
  { name: "test-a-change-desktop.png", url: "/i/followup-email/test", width: 1280, height: 900 },
];

const narrowPages = ["/", "/how-it-works", "/i/followup-email", "/i/followup-email?tab=versions",
  "/i/followup-email?tab=examples", "/i/followup-email?tab=good", "/i/partner-outreach", "/i/webinar-invite",
  "/i/followup-email/test", NOT_READY, NOT_READY + "&tab=examples", NOT_READY + "&tab=changes",
  NOT_READY + "&tab=details", READY, "/i/partner-outreach/result?current=v2&new=v3",
  "/i/webinar-invite/result?current=v1&new=v2", EXAMPLE, "/prompts/new"];

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
