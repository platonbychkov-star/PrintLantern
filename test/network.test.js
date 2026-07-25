const test = require("node:test");
const assert = require("node:assert/strict");
const {
  listLanAddresses,
  normalizePort,
  resolvePublicAddress
} = require("../src/network");

test("listLanAddresses keeps external IPv4 interfaces", () => {
  const result = listLanAddresses({
    WiFi: [
      { family: "IPv4", internal: false, address: "192.168.1.25" },
      { family: "IPv6", internal: false, address: "::1" }
    ],
    Loopback: [{ family: "IPv4", internal: true, address: "127.0.0.1" }]
  });
  assert.deepEqual(result, [
    { adapter: "WiFi", address: "192.168.1.25", label: "WiFi · 192.168.1.25" }
  ]);
});

test("resolvePublicAddress honors a known preferred address", () => {
  const addresses = [
    { address: "10.0.0.2" },
    { address: "192.168.1.25" }
  ];
  assert.equal(resolvePublicAddress("192.168.1.25", addresses), "192.168.1.25");
  assert.equal(resolvePublicAddress("missing", addresses), "10.0.0.2");
  assert.equal(resolvePublicAddress("auto", []), "127.0.0.1");
});

test("normalizePort validates the safe user port range", () => {
  assert.equal(normalizePort("4876"), 4876);
  assert.throws(() => normalizePort(80));
  assert.throws(() => normalizePort(70000));
  assert.throws(() => normalizePort("not-a-port"));
});
