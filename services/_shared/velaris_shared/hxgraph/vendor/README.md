# Vendored third-party assets

## `d3.v7.9.0.min.js`

**Why it is here.** The HxGraph visualizer loaded D3 from `https://d3js.org`,
which means the graph screen is **dead on any install without internet** — an
air-gapped deployment, a customer's private network, and the kiosk image that
`VELARIS_OS.md` describes. It also made a third party's CDN a runtime
dependency of a sovereignty-first product: a page that ships its own data and
then fetches its renderer from somebody else's server.

**What it is.** D3 v7.9.0, unmodified, from the published release.

    version   7.9.0
    sha256    f2094bbf6141b359722c4fe454eb6c4b0f0e42cc10cc7af921fc158fceb86539
    license   ISC — Copyright 2010-2023 Mike Bostock (header intact in the file)

The hash was checked against **three independent mirrors** (cdnjs, jsDelivr,
unpkg) before the file was committed; all three agreed. Do the same on any
upgrade, and record the new hash here.

**Who serves it.** `GET /api/v1/graph/vendor/d3.js` in hxgraph-service, which
is the one door for both readers: the service-rendered page at
`/api/v1/graph/visualize`, and Studio's `/hxgraph` visualizer, whose iframe
points at the same absolute path. One copy, one version, one place to upgrade.
