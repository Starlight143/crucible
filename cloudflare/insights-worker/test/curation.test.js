// Unit tests for curation ops (src/admin.js): list staged, promote, reject,
// list/set contributors.  Uses an in-memory fake env modelling insight_events +
// contributors.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  listStaged,
  promoteEvents,
  rejectEvents,
  listContributors,
  setContributor,
} from '../src/admin.js';

function fakeEnv(initialEvents = []) {
  const events = initialEvents.map((e) => ({ ...e }));
  const contributors = new Map();

  const exec = (sql, args) => {
    if (
      sql.includes("UPDATE insight_events SET trust_state = 'approved'") &&
      sql.includes('content_id IN')
    ) {
      let n = 0;
      for (const e of events) {
        if (e.trust_state === 'staged' && args.includes(e.content_id)) {
          e.trust_state = 'approved';
          n++;
        }
      }
      return { meta: { changes: n } };
    }
    if (
      sql.includes("UPDATE insight_events SET trust_state = 'approved'") &&
      sql.includes('contributor_id = ?')
    ) {
      let n = 0;
      for (const e of events) {
        if (e.trust_state === 'staged' && e.contributor_id === args[0]) {
          e.trust_state = 'approved';
          n++;
        }
      }
      return { meta: { changes: n } };
    }
    if (sql.includes('DELETE FROM insight_events')) {
      let n = 0;
      for (let i = events.length - 1; i >= 0; i--) {
        if (events[i].trust_state === 'staged' && args.includes(events[i].content_id)) {
          events.splice(i, 1);
          n++;
        }
      }
      return { meta: { changes: n } };
    }
    if (sql.includes('INSERT INTO contributors')) {
      const [cid, rep, st] = args; // bind order: cid, rep, st, rep, st
      const existing =
        contributors.get(cid) || { contributor_id: cid, reputation: 0.0, status: 'active' };
      if (rep !== null && rep !== undefined) existing.reputation = rep;
      if (st !== null && st !== undefined) existing.status = st;
      contributors.set(cid, existing);
      return { meta: { changes: 1 } };
    }
    return { meta: { changes: 0 } };
  };

  const query = (sql) => {
    if (sql.includes('FROM insight_events') && sql.includes("trust_state = 'staged'")) {
      return events.filter((e) => e.trust_state === 'staged').map((e) => ({ ...e }));
    }
    if (sql.includes('FROM contributors')) {
      return Array.from(contributors.values()).map((c) => ({ ...c }));
    }
    return [];
  };

  const DB = {
    prepare(sql) {
      const mk = (args = []) => ({
        bind: (...a) => mk(a),
        async run() {
          return exec(sql, args);
        },
        async all() {
          return { results: query(sql) };
        },
        async first() {
          return null;
        },
      });
      return mk();
    },
  };
  return { DB, _events: events, _contributors: contributors };
}

function staged(cid, contributor = 'alice') {
  return {
    content_id: cid,
    stream: 'output',
    ts: '2026-01-01T00:00:00Z',
    run_id: 'r',
    project_name: 'p',
    mode: 'Quant',
    kind: 'k',
    contributor_id: contributor,
    trust_state: 'staged',
  };
}

test('listStaged returns only staged rows', async () => {
  const env = fakeEnv([staged('c1'), { ...staged('c2'), trust_state: 'approved' }]);
  const r = await listStaged(env, {});
  assert.equal(r.ok, true);
  assert.equal(r.staged.length, 1);
  assert.equal(r.staged[0].content_id, 'c1');
});

test('promoteEvents by content_ids only promotes those', async () => {
  const env = fakeEnv([staged('c1'), staged('c2')]);
  const r = await promoteEvents(env, { contentIds: ['c1'] });
  assert.equal(r.promoted, 1);
  assert.equal(env._events.find((e) => e.content_id === 'c1').trust_state, 'approved');
  assert.equal(env._events.find((e) => e.content_id === 'c2').trust_state, 'staged');
});

test('promoteEvents by contributor promotes all their staged', async () => {
  const env = fakeEnv([staged('c1', 'bob'), staged('c2', 'bob'), staged('c3', 'alice')]);
  const r = await promoteEvents(env, { contributorId: 'bob' });
  assert.equal(r.promoted, 2);
});

test('promoteEvents with no selector → ok:false', async () => {
  assert.equal((await promoteEvents(fakeEnv([]), {})).ok, false);
});

test('rejectEvents deletes staged by id', async () => {
  const env = fakeEnv([staged('c1'), staged('c2')]);
  const r = await rejectEvents(env, { contentIds: ['c1'] });
  assert.equal(r.rejected, 1);
  assert.equal(env._events.length, 1);
});

test('rejectEvents requires content_ids', async () => {
  assert.equal((await rejectEvents(fakeEnv([]), {})).ok, false);
});

test('setContributor upserts; listContributors reflects it', async () => {
  const env = fakeEnv([]);
  const r = await setContributor(env, 'mallory', { reputation: 0.1, status: 'banned' });
  assert.equal(r.ok, true);
  const list = await listContributors(env);
  const row = list.contributors.find((c) => c.contributor_id === 'mallory');
  assert.equal(row.status, 'banned');
  assert.equal(row.reputation, 0.1);
});

test('setContributor with empty patch → ok:false', async () => {
  assert.equal((await setContributor(fakeEnv([]), 'x', {})).ok, false);
});
