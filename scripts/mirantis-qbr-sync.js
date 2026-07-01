#!/usr/bin/env node
/**
 * mirantis-qbr-sync — Salesforce → qbr.data.json
 * -----------------------------------------------
 * SCAFFOLD ONLY. Hand to RevOps / IT to wire up the real SF calls.
 *
 * Usage (target):
 *   node mirantis-qbr-sync.js --account "Vertex Logistics" --quarter "Q3 FY26" \
 *        --out ./accounts/vertex-q3fy26.json
 *
 * Env vars required:
 *   SF_LOGIN_URL          e.g. https://your-org.my.salesforce.com
 *   SF_CLIENT_ID          OAuth connected app client id
 *   SF_CLIENT_SECRET
 *   SF_USERNAME           service-account username
 *   SF_PASSWORD           password + security token concatenated
 *   ZENDESK_TOKEN         (optional) for support metrics
 *   DATADOG_API_KEY       (optional) for usage telemetry
 */

const fs = require('fs');
const path = require('path');

// ---------------------------------------------------------------------------
// 1. Parse args
// ---------------------------------------------------------------------------
const args = Object.fromEntries(
  process.argv.slice(2).reduce((acc, a, i, arr) => {
    if (a.startsWith('--')) acc.push([a.slice(2), arr[i + 1]]);
    return acc;
  }, [])
);
if (!args.account) { console.error('--account required'); process.exit(1); }
const accountName = args.account;
const quarter = args.quarter || inferCurrentQuarter();
const outPath = args.out || `./accounts/${slug(accountName)}-${slug(quarter)}.json`;

// ---------------------------------------------------------------------------
// 2. Salesforce — get OAuth token, run SOQL queries
//    Implement using jsforce, simple-salesforce, or raw REST. Example with fetch:
// ---------------------------------------------------------------------------
async function sfLogin() {
  // TODO: implement OAuth password flow OR JWT bearer flow
  // const body = new URLSearchParams({ grant_type: 'password', client_id, client_secret, username, password });
  // const r = await fetch(`${SF_LOGIN_URL}/services/oauth2/token`, { method: 'POST', body });
  // return await r.json();   // { access_token, instance_url, ... }
  throw new Error('TODO: implement SF OAuth — talk to Mirantis IT for the Connected App');
}

async function sfQuery(token, soql) {
  // const r = await fetch(`${token.instance_url}/services/data/v60.0/query?q=${encodeURIComponent(soql)}`,
  //                       { headers: { Authorization: `Bearer ${token.access_token}` } });
  // return (await r.json()).records;
  throw new Error('TODO');
}

// SOQL queries we need
const SOQL_ACCOUNT = (name) => `
  SELECT Id, Name, Tier__c, Industry, ARR__c, ARR_Prior__c, Renewal_Date__c, Owner.Name, Owner.Email
  FROM Account
  WHERE Name = '${name.replace(/'/g, "\\'")}'
  LIMIT 1
`;
const SOQL_OPPS = (accountId) => `
  SELECT Name, Amount, CloseDate, StageName, Type
  FROM Opportunity
  WHERE AccountId = '${accountId}' AND IsClosed = false
  ORDER BY CloseDate ASC
`;
const SOQL_PRODUCTS = (accountId) => `
  SELECT Product2.Name, Quantity, UnitOfMeasure__c
  FROM Asset
  WHERE AccountId = '${accountId}' AND Status = 'Active'
`;

// ---------------------------------------------------------------------------
// 3. Zendesk / Salesforce Service Cloud — support metrics
// ---------------------------------------------------------------------------
async function fetchSupport(accountId) {
  // GET /api/v2/search.json?query=organization:{org_id} type:ticket created>90d
  return { ticketsTotal: 0, p1Count: 0, slaMetPct: 0, p1MttrHours: 0, csat: 0,
           ticketsBySeverity: { p1: 0, p2: 0, p3: 0, p4: 0 } };
}

// ---------------------------------------------------------------------------
// 4. Datadog / Grafana — usage telemetry
// ---------------------------------------------------------------------------
async function fetchUsage(accountId) {
  // Query the customer's namespace/tag in Datadog for cluster/node/workload counts
  return { clusters: 0, clustersDelta: 0, nodes: 0, nodesDelta: 0,
           workloads: 0, workloadsDelta: 0, environments: 0, uptime: 99.9 };
}

// ---------------------------------------------------------------------------
// 5. Build the QBR JSON
// ---------------------------------------------------------------------------
async function build() {
  const token = await sfLogin();
  const [acct] = await sfQuery(token, SOQL_ACCOUNT(accountName));
  if (!acct) throw new Error(`Account "${accountName}" not found in SF`);

  const opps = await sfQuery(token, SOQL_OPPS(acct.Id));
  const products = await sfQuery(token, SOQL_PRODUCTS(acct.Id));
  const support = await fetchSupport(acct.Id);
  const usage = await fetchUsage(acct.Id);

  const payload = {
    _meta: {
      schemaVersion: 'qbr-2026.06',
      lastUpdated: new Date().toISOString(),
      source: 'mirantis-qbr-sync v0.1',
    },
    customer: {
      name: acct.Name,
      tier: acct.Tier__c || 'Strategic',
      industry: acct.Industry || '',
      stakeholders: [],   // TODO: pull from SF Contacts where IsKeyContact__c = true
    },
    quarter,
    preparedBy: acct.Owner?.Name || '',
    preparedByEmail: acct.Owner?.Email || '',
    presentationDate: new Date().toISOString().slice(0, 10),
    nextQbr: { label: `${nextQuarter(quarter)} review`, date: '' },
    commercial: {
      arr: {
        current: acct.ARR__c || 0,
        prior: acct.ARR_Prior__c || 0,
        yoyPct: yoyPct(acct.ARR__c, acct.ARR_Prior__c),
      },
      renewalDate: acct.Renewal_Date__c || '',
      renewalSponsor: 'TBD — confirm in meeting',
      expansions: opps.map(o => ({
        name: o.Name,
        valueUSD: o.Amount,
        quarter: quarterFromDate(o.CloseDate),
      })),
    },
    usage,
    support,
    nps: { score: 0, industry: 30, delta: 0 },   // TODO: pull from Delighted / Wootric
    products: products.map(p => p.Product2.Name),
    productMix: products.map(p => ({
      product: p.Product2.Name,
      entitlement: `${p.Quantity} ${p.UnitOfMeasure__c || ''}`,
      inUse: '',          // TODO: cross-reference with telemetry
      utilizationPct: 0,
      trend: '— flat',
    })),

    // The narrative pieces — incidents, wins, risks, roadmaps — stay manual.
    // SF doesn't have this data structured enough to auto-fill, and the TAM's
    // judgment matters too much to robot-generate.
    incidents: [],
    wins: [],
    risks: [],
    mirantisRoadmap: [],   // Pull from product mgmt's quarterly roadmap doc instead
    customerRoadmap: [],
    training: { delivered: [], planned: [], deliveredNote: '', plannedNote: '' },
    execSummaryTakeaways: [],
    asks: { fromUs: [], fromYou: [] },
    nextActions: [],
    previousActions: [],   // TODO: pull from last quarter's qbr.data.json if it exists
  };

  fs.mkdirSync(path.dirname(outPath), { recursive: true });
  fs.writeFileSync(outPath, JSON.stringify(payload, null, 2));
  console.log(`✓ Wrote ${outPath}`);
  console.log(`  Open the deck and set the dataFile tweak to: ${outPath}`);
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function slug(s) { return s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, ''); }
function yoyPct(c, p) { return (!c || !p) ? 0 : Math.round(((c - p) / p) * 100); }
function inferCurrentQuarter() {
  const m = new Date().getMonth();
  return `Q${Math.floor(m / 3) + 1} FY${new Date().getFullYear()}`;
}
function nextQuarter(q) {
  const m = q.match(/Q(\d)\s+FY(\d+)/);
  if (!m) return q;
  const n = parseInt(m[1]) + 1;
  return n > 4 ? `Q1 FY${parseInt(m[2]) + 1}` : `Q${n} FY${m[2]}`;
}
function quarterFromDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return `Q${Math.floor(d.getMonth() / 3) + 1} FY${d.getFullYear()}`;
}

build().catch(e => { console.error('FAIL:', e.message); process.exit(1); });
