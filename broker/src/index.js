const GITHUB_API = "https://api.github.com";
const OIDC_ISSUER = "https://token.actions.githubusercontent.com";
const OIDC_JWKS = `${OIDC_ISSUER}/.well-known/jwks`;
const TOKEN_TTL_SECONDS = 540;

let cachedJwks;

class BrokerError extends Error {
    constructor(status, message) {
        super(message);
        this.status = status;
    }
}

function json(status, body) {
    return new Response(JSON.stringify(body), {
        status,
        headers: {
            "cache-control": "no-store",
            "content-type": "application/json; charset=utf-8",
            "x-content-type-options": "nosniff",
        },
    });
}

function decodeBase64Url(value) {
    const padded = value
        .replaceAll("-", "+")
        .replaceAll("_", "/")
        .padEnd(Math.ceil(value.length / 4) * 4, "=");
    return Uint8Array.from(atob(padded), (character) =>
        character.charCodeAt(0),
    );
}

function decodeJson(value) {
    try {
        return JSON.parse(new TextDecoder().decode(decodeBase64Url(value)));
    } catch {
        throw new BrokerError(401, "the GitHub identity token is not valid");
    }
}

function audienceMatches(actual, expected) {
    return Array.isArray(actual)
        ? actual.includes(expected)
        : actual === expected;
}

async function oidcKeys(fetcher, force = false) {
    const now = Date.now();
    if (!force && cachedJwks && cachedJwks.expiresAt > now)
        return cachedJwks.keys;
    const response = await fetcher(OIDC_JWKS, {
        headers: { accept: "application/json" },
    });
    if (!response.ok)
        throw new BrokerError(503, "GitHub identity keys are unavailable");
    const body = await response.json();
    if (!Array.isArray(body.keys)) {
        throw new BrokerError(503, "GitHub identity keys are unavailable");
    }
    cachedJwks = { keys: body.keys, expiresAt: now + 60 * 60 * 1000 };
    return body.keys;
}

export async function verifyOidc(
    token,
    env,
    fetcher = fetch,
    now = Date.now() / 1000,
) {
    const segments = token.split(".");
    if (segments.length !== 3) {
        throw new BrokerError(401, "the GitHub identity token is not valid");
    }
    const [encodedHeader, encodedClaims, encodedSignature] = segments;
    const header = decodeJson(encodedHeader);
    const claims = decodeJson(encodedClaims);
    if (header.alg !== "RS256" || typeof header.kid !== "string") {
        throw new BrokerError(401, "the GitHub identity token is not valid");
    }
    let jwk = (await oidcKeys(fetcher)).find(
        (candidate) => candidate.kid === header.kid,
    );
    if (!jwk) {
        jwk = (await oidcKeys(fetcher, true)).find(
            (candidate) => candidate.kid === header.kid,
        );
    }
    if (!jwk)
        throw new BrokerError(401, "the GitHub identity token is not valid");
    const key = await crypto.subtle.importKey(
        "jwk",
        jwk,
        { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
        false,
        ["verify"],
    );
    const signed = new TextEncoder().encode(
        `${encodedHeader}.${encodedClaims}`,
    );
    const valid = await crypto.subtle.verify(
        "RSASSA-PKCS1-v1_5",
        key,
        decodeBase64Url(encodedSignature),
        signed,
    );
    const requiredStrings = [
        "repository",
        "repository_owner",
        "repository_id",
        "job_workflow_ref",
    ];
    if (
        !valid ||
        claims.iss !== OIDC_ISSUER ||
        !audienceMatches(claims.aud, env.OIDC_AUDIENCE) ||
        typeof claims.exp !== "number" ||
        claims.exp <= now ||
        (typeof claims.nbf === "number" && claims.nbf > now) ||
        requiredStrings.some(
            (name) => typeof claims[name] !== "string" || !claims[name],
        ) ||
        claims.job_workflow_ref !== env.TRUSTED_WORKFLOW
    ) {
        throw new BrokerError(
            403,
            "this workflow is not allowed to use the family app",
        );
    }
    const [owner, repository] = claims.repository.split("/");
    if (!owner || !repository || claims.repository.split("/").length !== 2) {
        throw new BrokerError(
            403,
            "this workflow is not allowed to use the family app",
        );
    }
    if (owner.toLowerCase() !== claims.repository_owner.toLowerCase()) {
        throw new BrokerError(
            403,
            "this workflow is not allowed to use the family app",
        );
    }
    return claims;
}

function base64Url(bytes) {
    let binary = "";
    for (const byte of bytes) binary += String.fromCharCode(byte);
    return btoa(binary)
        .replaceAll("+", "-")
        .replaceAll("/", "_")
        .replaceAll("=", "");
}

function derLength(length) {
    if (length < 128) return Uint8Array.of(length);
    const bytes = [];
    for (let remaining = length; remaining; remaining >>>= 8)
        bytes.unshift(remaining & 255);
    return Uint8Array.of(0x80 | bytes.length, ...bytes);
}

function der(tag, value) {
    return Uint8Array.of(tag, ...derLength(value.length), ...value);
}

function pemBytes(pem, label) {
    const match = pem.match(
        new RegExp(
            `-----BEGIN ${label}-----([\\s\\S]+?)-----END ${label}-----`,
        ),
    );
    if (!match) return null;
    return Uint8Array.from(atob(match[1].replaceAll(/\s/g, "")), (character) =>
        character.charCodeAt(0),
    );
}

function privateKeyDer(pem) {
    const pkcs8 = pemBytes(pem, "PRIVATE KEY");
    if (pkcs8) return pkcs8;
    const pkcs1 = pemBytes(pem, "RSA PRIVATE KEY");
    if (!pkcs1) throw new BrokerError(500, "the broker is not configured");
    const version = Uint8Array.of(0x02, 0x01, 0x00);
    const rsaAlgorithm = Uint8Array.of(
        0x30,
        0x0d,
        0x06,
        0x09,
        0x2a,
        0x86,
        0x48,
        0x86,
        0xf7,
        0x0d,
        0x01,
        0x01,
        0x01,
        0x05,
        0x00,
    );
    return der(
        0x30,
        Uint8Array.of(...version, ...rsaAlgorithm, ...der(0x04, pkcs1)),
    );
}

async function appJwt(env, now = Math.floor(Date.now() / 1000)) {
    if (!env.APP_CLIENT_ID || !env.APP_PRIVATE_KEY) {
        throw new BrokerError(500, "the broker is not configured");
    }
    const key = await crypto.subtle.importKey(
        "pkcs8",
        privateKeyDer(env.APP_PRIVATE_KEY),
        { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
        false,
        ["sign"],
    );
    const header = base64Url(
        new TextEncoder().encode(JSON.stringify({ alg: "RS256", typ: "JWT" })),
    );
    const claims = base64Url(
        new TextEncoder().encode(
            JSON.stringify({
                iat: now - 60,
                exp: now + TOKEN_TTL_SECONDS,
                iss: env.APP_CLIENT_ID,
            }),
        ),
    );
    const input = `${header}.${claims}`;
    const signature = await crypto.subtle.sign(
        "RSASSA-PKCS1-v1_5",
        key,
        new TextEncoder().encode(input),
    );
    return `${input}.${base64Url(new Uint8Array(signature))}`;
}

async function github(fetcher, path, jwt, init = {}) {
    const response = await fetcher(`${GITHUB_API}${path}`, {
        ...init,
        headers: {
            accept: "application/vnd.github+json",
            authorization: `Bearer ${jwt}`,
            "content-type": "application/json",
            "user-agent": "lucy-assistant-family-ci-token-broker",
            "x-github-api-version": "2022-11-28",
            ...init.headers,
        },
    });
    const body = await response.json().catch(() => ({}));
    return { response, body };
}

async function installationFor(owner, jwt, fetcher) {
    for (const kind of ["users", "orgs"]) {
        const { response, body } = await github(
            fetcher,
            `/${kind}/${owner}/installation`,
            jwt,
        );
        if (response.ok) return body;
        if (response.status !== 404) {
            throw new BrokerError(
                502,
                "GitHub could not confirm the app installation",
            );
        }
    }
    throw new BrokerError(
        403,
        "install the family app on this repository owner first",
    );
}

export async function handle(request, env, fetcher = fetch) {
    if (
        request.method === "GET" &&
        new URL(request.url).pathname === "/healthy"
    ) {
        return json(200, { status: "ok" });
    }
    if (
        request.method !== "POST" ||
        new URL(request.url).pathname !== "/v1/token"
    ) {
        return json(404, { error: "not found" });
    }
    const authorization = request.headers.get("authorization") || "";
    if (!authorization.startsWith("Bearer ") || authorization.length > 20_000) {
        return json(401, { error: "a GitHub identity token is required" });
    }
    try {
        const claims = await verifyOidc(authorization.slice(7), env, fetcher);
        const jwt = await appJwt(env);
        const installation = await installationFor(
            claims.repository_owner,
            jwt,
            fetcher,
        );
        // The token covers exactly the repositories the owner chose when installing the
        // app, read-only. Naming repositories here would 422 for an owner who does not
        // have copies of the hubs, and the owner already drew the boundary at install.
        const { response, body: token } = await github(
            fetcher,
            `/app/installations/${installation.id}/access_tokens`,
            jwt,
            {
                method: "POST",
                body: JSON.stringify({
                    permissions: { contents: "read", metadata: "read" },
                }),
            },
        );
        if (!response.ok || typeof token.token !== "string") {
            const status = response.status === 422 ? 403 : 502;
            throw new BrokerError(
                status,
                "GitHub could not mint the installation token",
            );
        }
        return json(200, { token: token.token, expires_at: token.expires_at });
    } catch (error) {
        if (error instanceof BrokerError)
            return json(error.status, { error: error.message });
        return json(500, { error: "the token broker failed" });
    }
}

export default {
    fetch(request, env) {
        return handle(request, env);
    },
};
