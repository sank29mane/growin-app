"""Mac-side India pilot data package.

This package builds the trustworthy daily-bar store for the India pilot: NSE
bhavcopy ingestion, security-master identity, cross-checks, quarantine and
dataset snapshots. It never talks to the broker data API directly. All broker
data arrives through the VM relay, and nothing in this package opens a broker
session, reads credentials or addresses a broker host.
"""
