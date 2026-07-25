const test = require("node:test");
const assert = require("node:assert/strict");
const { normalizeSnapshot, toArray } = require("../src/printer-service");

test("toArray normalizes PowerShell singleton JSON values", () => {
  assert.deepEqual(toArray(null), []);
  assert.deepEqual(toArray({ ID: 1 }), [{ ID: 1 }]);
  assert.deepEqual(toArray([{ ID: 1 }]), [{ ID: 1 }]);
});

test("normalizeSnapshot maps and sorts jobs", () => {
  const result = normalizeSnapshot({
    printers: { Name: "Office", PrinterStatus: "Normal" },
    jobs: [
      {
        ID: 1,
        PrinterName: "Office",
        DocumentName: "old.pdf",
        SubmittedTime: "2026-07-24T10:00:00Z"
      },
      {
        ID: 2,
        PrinterName: "Office",
        DocumentName: "new.pdf",
        SubmittedTime: "2026-07-24T11:00:00Z",
        TotalPages: 3,
        PagesPrinted: 1
      }
    ]
  });

  assert.equal(result.printers[0].name, "Office");
  assert.equal(result.jobs[0].documentName, "new.pdf");
  assert.equal(result.jobs[0].totalPages, 3);
});
