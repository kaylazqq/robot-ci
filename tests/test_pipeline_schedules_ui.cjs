const assert = require("assert");
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "web", "index.html"), "utf8");
const app = fs.readFileSync(path.join(root, "web", "app.js"), "utf8");

assert(html.includes('id="pipelineScheduleBlock"'));
assert(html.includes('id="pipelineScheduleList"'));
assert(html.includes('id="btnScheduleCreate"'));
assert(app.includes('block.hidden = runLayerMode !== "edit"'));
assert(app.includes("async function loadPipelineSchedules"));
assert(app.includes("function openScheduleEditor"));
assert(app.includes('"/run-now"'));
assert(app.includes('bind("btnScheduleCreate"'));

console.log("pipeline schedule UI checks passed");
