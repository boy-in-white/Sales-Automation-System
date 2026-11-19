#!/usr/bin/env python3
"""Preview legacy stock/product label repairs; --apply explicitly writes them."""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from product_link_repair import repair_product_links  # noqa: E402
from db_runtime import close_pools  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--order-id', action='append', default=[])
    args = parser.parse_args()
    try:
        changes = repair_product_links(order_ids=args.order_id, apply=args.apply)
        for item in changes:
            print('%s -> %s (%s)' % (item['raw_product_name'], item['product_name'], item['order_id']))
        print('%s: %s product link(s); stock, money and write-offs unchanged' % ('Applied' if args.apply else 'Preview', len(changes)))
    finally:
        close_pools()


if __name__ == '__main__':
    main()
