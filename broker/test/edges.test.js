import assert from "node:assert/strict";
import { generateKeyPairSync } from "node:crypto";
import { mock, test } from "node:test";

import broker, { handle, verifyOidc } from "../src/index.js";

const encoder = new TextEncoder();
const audience = "lucy-assistant-family-ci";
const workflow =
    "tochi-mba/LUCY-assistant/.github/workflows/service.yml@refs/tags/v1";
const jwksUrl = "https://token.actions.githubusercontent.com/.well-known/jwks";
const installationUrl = "https://api.github.com/users/alice/installation";
const mintUrl = "https://api.github.com/app/installations/99/access_tokens";
let fixtureNumber = 0;

function base64Url(value) {
    const bytes =
        typeof value === "string"
            ? encoder.encode(value)
            : new Uint8Array(value);
    return Buffer.from(bytes).toString("base64url");
}

function pemOf(label, bytes) {
    return [
        `-----BEGIN ${label}-----`,
        Buffer.from(bytes)
            .toString("base64")
            .match(/.{1,64}/g)
            .join("\n"),
        `-----END ${label}-----`,
    ].join("\n");
}

const rsa = {
    name: "RSASSA-PKCS1-v1_5",
    modulusLength: 2048,
    publicExponent: new Uint8Array([1, 0, 1]),
    hash: "SHA-256",
};

// A fixture whose GitHub answers can be replaced one URL at a time.
async function fixture(routes = {}) {
    const kid = `edge-key-${fixtureNumber++}`;
    const oidcKeys = await crypto.subtle.generateKey(rsa, true, [
        "sign",
        "verify",
    ]);
    const appKeys = await crypto.subtle.generateKey(rsa, true, [
        "sign",
        "verify",
    ]);
    const publicJwk = await crypto.subtle.exportKey("jwk", oidcKeys.publicKey);
    const env = {
        APP_CLIENT_ID: "Iv23example",
        APP_PRIVATE_KEY: pemOf(
            "PRIVATE KEY",
            await crypto.subtle.exportKey("pkcs8", appKeys.privateKey),
        ),
        OIDC_AUDIENCE: audience,
        TRUSTED_WORKFLOW: workflow,
    };

    async function token(overrides = {}, header = { alg: "RS256", kid }) {
        const now = Math.floor(Date.now() / 1000);
        const input = `${base64Url(JSON.stringify(header))}.${base64Url(
            JSON.stringify({
                iss: "https://token.actions.githubusercontent.com",
                aud: audience,
                exp: now + 300,
                repository: "alice/Persona-api",
                repository_owner: "alice",
                repository_id: "123",
                job_workflow_ref: workflow,
                ...overrides,
            }),
        )}`;
        const signature = await crypto.subtle.sign(
            "RSASSA-PKCS1-v1_5",
            oidcKeys.privateKey,
            encoder.encode(input),
        );
        return `${input}.${base64Url(signature)}`;
    }

    const defaults = {
        [jwksUrl]: () =>
            Response.json({ keys: [{ ...publicJwk, kid, alg: "RS256" }] }),
        [installationUrl]: () => Response.json({ id: 99 }),
        [mintUrl]: () =>
            Response.json(
                { token: "ghs_edge", expires_at: "2026-09-16T16:00:00Z" },
                { status: 201 },
            ),
    };
    async function fetcher(url) {
        const route = routes[url] ?? defaults[url];
        return route
            ? route()
            : Response.json({ message: "not found" }, { status: 404 });
    }
    return { env, fetcher, token };
}

function request(token) {
    return new Request("https://broker.example/v1/token", {
        method: "POST",
        headers: { authorization: `Bearer ${token}` },
    });
}

async function outcome(f, token, env = f.env) {
    const response = await handle(request(token), env, f.fetcher);
    return { status: response.status, body: await response.json() };
}

const notAllowed = {
    error: "this workflow is not allowed to use the family app",
};
const notValid = { error: "the GitHub identity token is not valid" };

test("refuses tokens that are not three segments or do not decode", async () => {
    const f = await fixture();
    assert.deepEqual(await outcome(f, "only.two"), {
        status: 401,
        body: notValid,
    });
    assert.deepEqual(await outcome(f, "!!!.e30.sig"), {
        status: 401,
        body: notValid,
    });
});

test("refuses a header that is not RS256 with a key id", async () => {
    const f = await fixture();
    for (const header of [{ alg: "HS256", kid: "x" }, { alg: "RS256" }]) {
        assert.deepEqual(await outcome(f, await f.token({}, header)), {
            status: 401,
            body: notValid,
        });
    }
});

test("refuses a key id GitHub does not publish, even after a refresh", async () => {
    const f = await fixture();
    const unknown = await f.token({}, { alg: "RS256", kid: "nobody" });
    assert.deepEqual(await outcome(f, unknown), {
        status: 401,
        body: notValid,
    });
});

test("reports GitHub's identity keys as unavailable when it cannot serve them", async () => {
    const unavailable = {
        error: "GitHub identity keys are unavailable",
    };
    for (const answer of [
        () => new Response("down", { status: 503 }),
        () => Response.json({ keys: "not a list" }),
    ]) {
        const f = await fixture({ [jwksUrl]: answer });
        assert.deepEqual(await outcome(f, await f.token()), {
            status: 503,
            body: unavailable,
        });
    }
});

test("serves keys from the cache until it expires, then fetches again", async () => {
    const f = await fixture();
    let fetched = 0;
    const counting = async (url, init) => {
        if (url === jwksUrl) fetched++;
        return f.fetcher(url, init);
    };
    const identity = await f.token();
    await verifyOidc(identity, f.env, counting);
    await verifyOidc(identity, f.env, counting);
    assert.equal(fetched, 1);
    // The cache reads the wall clock; the token's own expiry is checked against `now`.
    const now = Date.now() / 1000;
    const later = Date.now() + 2 * 60 * 60 * 1000;
    mock.method(Date, "now", () => later);
    try {
        await verifyOidc(identity, f.env, counting, now);
    } finally {
        mock.restoreAll();
    }
    assert.equal(fetched, 2);
});

test("accepts an audience list that names the broker and honours not-before", async () => {
    const f = await fixture();
    const listed = await f.token({ aud: ["elsewhere", audience] });
    assert.equal((await outcome(f, listed)).status, 200);
    const early = await f.token({ nbf: Math.floor(Date.now() / 1000) + 600 });
    assert.deepEqual(await outcome(f, early), {
        status: 403,
        body: notAllowed,
    });
    const settled = await f.token({ nbf: 1 });
    assert.equal((await outcome(f, settled)).status, 200);
});

test("refuses tokens with missing claims or a repository that is not owner/name", async () => {
    const f = await fixture();
    for (const claims of [
        { iss: "https://elsewhere.example" },
        { exp: "soon" },
        { repository_id: "" },
        { repository_id: 123 },
        { repository: "alice" },
        { repository: "/Persona-api", repository_owner: "" },
        { repository: "alice/" },
        { repository: "alice/a/b" },
        { repository: "mallory/Persona-api" },
    ]) {
        assert.deepEqual(
            await outcome(f, await f.token(claims)),
            { status: 403, body: notAllowed },
            JSON.stringify(claims),
        );
    }
    const otherCase = await f.token({ repository: "Alice/Persona-api" });
    assert.equal((await outcome(f, otherCase)).status, 200);
});

test("refuses an oversized authorization header without verifying it", async () => {
    const f = await fixture();
    assert.equal((await outcome(f, "x".repeat(20_001))).status, 401);
});

test("signs with a PKCS#1 app key as well as a PKCS#8 one", async () => {
    const f = await fixture();
    const { privateKey } = generateKeyPairSync("rsa", {
        modulusLength: 2048,
        privateKeyEncoding: { type: "pkcs1", format: "pem" },
        publicKeyEncoding: { type: "spki", format: "pem" },
    });
    const env = { ...f.env, APP_PRIVATE_KEY: privateKey };
    assert.deepEqual(await outcome(f, await f.token(), env), {
        status: 200,
        body: { token: "ghs_edge", expires_at: "2026-09-16T16:00:00Z" },
    });
});

test("reports a broker without its app settings as not configured", async () => {
    const f = await fixture();
    const notConfigured = { error: "the broker is not configured" };
    for (const env of [
        { ...f.env, APP_CLIENT_ID: "" },
        { ...f.env, APP_PRIVATE_KEY: undefined },
        { ...f.env, APP_PRIVATE_KEY: "not a pem at all" },
    ]) {
        assert.deepEqual(await outcome(f, await f.token(), env), {
            status: 500,
            body: notConfigured,
        });
    }
});

test("hides unexpected failures behind a generic error", async () => {
    const f = await fixture();
    // A PKCS#1 block too short to be a key: it wraps in a short-form length and then
    // fails to import, which is not a BrokerError.
    const env = {
        ...f.env,
        APP_PRIVATE_KEY: pemOf("RSA PRIVATE KEY", Uint8Array.of(1, 2, 3)),
    };
    assert.deepEqual(await outcome(f, await f.token(), env), {
        status: 500,
        body: { error: "the token broker failed" },
    });
});

test("finds an organisation's installation when the owner is not a user", async () => {
    const f = await fixture({
        [installationUrl]: () => Response.json({}, { status: 404 }),
        "https://api.github.com/orgs/alice/installation": () =>
            Response.json({ id: 99 }),
    });
    assert.equal((await outcome(f, await f.token())).status, 200);
});

test("reports GitHub failures while confirming the installation", async () => {
    const f = await fixture({
        [installationUrl]: () => new Response("<html>", { status: 500 }),
    });
    assert.deepEqual(await outcome(f, await f.token()), {
        status: 502,
        body: { error: "GitHub could not confirm the app installation" },
    });
});

test("translates a failed or empty mint", async () => {
    const cases = [
        [() => Response.json({}, { status: 422 }), 403],
        [() => Response.json({}, { status: 500 }), 502],
        [() => Response.json({ token: 42 }, { status: 201 }), 502],
    ];
    for (const [answer, status] of cases) {
        const f = await fixture({ [mintUrl]: answer });
        assert.deepEqual(await outcome(f, await f.token()), {
            status,
            body: { error: "GitHub could not mint the installation token" },
        });
    }
});

test("the worker entry point serves requests through handle", async () => {
    const response = await broker.fetch(
        new Request("https://broker.example/healthy"),
        {},
    );
    assert.deepEqual(await response.json(), { status: "ok" });
    assert.equal(response.headers.get("cache-control"), "no-store");
});
