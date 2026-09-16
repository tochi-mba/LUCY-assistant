import assert from "node:assert/strict";
import { test } from "node:test";

import { handle } from "../src/index.js";

const encoder = new TextEncoder();
const audience = "lucy-assistant-family-ci";
const workflow =
    "tochi-mba/LUCY-assistant/.github/workflows/service.yml@refs/tags/v1";
let fixtureNumber = 0;

function base64Url(value) {
    const bytes =
        typeof value === "string"
            ? encoder.encode(value)
            : new Uint8Array(value);
    return Buffer.from(bytes).toString("base64url");
}

async function fixture() {
    const kid = `test-key-${fixtureNumber++}`;
    const oidcKeys = await crypto.subtle.generateKey(
        {
            name: "RSASSA-PKCS1-v1_5",
            modulusLength: 2048,
            publicExponent: new Uint8Array([1, 0, 1]),
            hash: "SHA-256",
        },
        true,
        ["sign", "verify"],
    );
    const appKeys = await crypto.subtle.generateKey(
        {
            name: "RSASSA-PKCS1-v1_5",
            modulusLength: 2048,
            publicExponent: new Uint8Array([1, 0, 1]),
            hash: "SHA-256",
        },
        true,
        ["sign", "verify"],
    );
    const publicJwk = await crypto.subtle.exportKey("jwk", oidcKeys.publicKey);
    const privateDer = await crypto.subtle.exportKey(
        "pkcs8",
        appKeys.privateKey,
    );
    const pem = [
        "-----BEGIN PRIVATE KEY-----",
        Buffer.from(privateDer)
            .toString("base64")
            .match(/.{1,64}/g)
            .join("\n"),
        "-----END PRIVATE KEY-----",
    ].join("\n");
    const env = {
        APP_CLIENT_ID: "Iv23example",
        APP_PRIVATE_KEY: pem,
        OIDC_AUDIENCE: audience,
        TRUSTED_WORKFLOW: workflow,
    };

    async function token(overrides = {}) {
        const now = Math.floor(Date.now() / 1000);
        const header = base64Url(JSON.stringify({ alg: "RS256", kid }));
        const claims = base64Url(
            JSON.stringify({
                iss: "https://token.actions.githubusercontent.com",
                aud: audience,
                iat: now - 10,
                exp: now + 300,
                repository: "alice/Persona-api",
                repository_owner: "alice",
                repository_id: "123",
                job_workflow_ref: workflow,
                ...overrides,
            }),
        );
        const input = `${header}.${claims}`;
        const signature = await crypto.subtle.sign(
            "RSASSA-PKCS1-v1_5",
            oidcKeys.privateKey,
            encoder.encode(input),
        );
        return `${input}.${base64Url(signature)}`;
    }

    const calls = [];
    async function fetcher(url, init = {}) {
        calls.push({ url: String(url), init });
        if (
            url ===
            "https://token.actions.githubusercontent.com/.well-known/jwks"
        ) {
            return Response.json({
                keys: [{ ...publicJwk, kid, alg: "RS256" }],
            });
        }
        if (url === "https://api.github.com/users/alice/installation") {
            return Response.json({ id: 99 });
        }
        if (
            url === "https://api.github.com/app/installations/99/access_tokens"
        ) {
            return Response.json(
                {
                    token: "ghs_test_result",
                    expires_at: "2026-09-16T16:00:00Z",
                },
                { status: 201 },
            );
        }
        return Response.json({ message: "not found" }, { status: 404 });
    }
    return { calls, env, fetcher, token };
}

function request(
    token,
    repositories = ["Keyring-api", "Settings-api", "LUCY-assistant"],
) {
    return new Request("https://broker.example/v1/token", {
        method: "POST",
        headers: {
            authorization: `Bearer ${token}`,
            "content-type": "application/json",
        },
        body: JSON.stringify({ repositories }),
    });
}

test("mints a token only for requested repositories under the caller's installation", async () => {
    const f = await fixture();
    const response = await handle(request(await f.token()), f.env, f.fetcher);
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), {
        token: "ghs_test_result",
        expires_at: "2026-09-16T16:00:00Z",
    });
    const mint = f.calls.find((call) => call.url.endsWith("/access_tokens"));
    assert.deepEqual(JSON.parse(mint.init.body), {
        repositories: [
            "Persona-api",
            "Keyring-api",
            "Settings-api",
            "LUCY-assistant",
        ],
        permissions: { contents: "read", metadata: "read" },
    });
    assert.match(
        mint.init.headers.authorization,
        /^Bearer [^.]+\.[^.]+\.[^.]+$/,
    );
    assert.doesNotMatch(JSON.stringify(f.calls), /ghs_test_result/);
});

test("rejects a token from an untrusted workflow", async () => {
    const f = await fixture();
    const response = await handle(
        request(
            await f.token({
                job_workflow_ref: "alice/evil/.github/workflows/steal.yml@main",
            }),
        ),
        f.env,
        f.fetcher,
    );
    assert.equal(response.status, 403);
    assert.deepEqual(await response.json(), {
        error: "this workflow is not allowed to use the family app",
    });
    assert.equal(
        f.calls.some((call) => call.url.includes("/access_tokens")),
        false,
    );
});

test("rejects the wrong audience, an expired token, and a forged signature", async () => {
    for (const claims of [{ aud: "somewhere-else" }, { exp: 1 }]) {
        const f = await fixture();
        const response = await handle(
            request(await f.token(claims)),
            f.env,
            f.fetcher,
        );
        assert.equal(response.status, 403);
    }
    const f = await fixture();
    const forged = `${await f.token()}broken`;
    assert.equal((await handle(request(forged), f.env, f.fetcher)).status, 403);
});

test("validates request shape without echoing input", async () => {
    const f = await fixture();
    const identity = await f.token();
    const bad = request(identity, ["valid", "../not-valid"]);
    const response = await handle(bad, f.env, f.fetcher);
    assert.equal(response.status, 400);
    assert.equal(
        JSON.stringify(await response.json()).includes("../not-valid"),
        false,
    );

    const missing = await handle(
        new Request("https://broker.example/v1/token", { method: "POST" }),
        f.env,
        f.fetcher,
    );
    assert.equal(missing.status, 415);
    assert.equal(
        (await handle(new Request("https://broker.example/nope"), f.env))
            .status,
        404,
    );
    assert.equal(
        (await handle(new Request("https://broker.example/healthy"), f.env))
            .status,
        200,
    );
});

test("requires the app installation and translates GitHub failures", async () => {
    const f = await fixture();
    const notInstalled = async (url, init) => {
        if (String(url).includes("token.actions")) return f.fetcher(url, init);
        return Response.json({ message: "not found" }, { status: 404 });
    };
    const response = await handle(
        request(await f.token()),
        f.env,
        notInstalled,
    );
    assert.equal(response.status, 403);
    assert.deepEqual(await response.json(), {
        error: "install the family app on this repository owner first",
    });
});
