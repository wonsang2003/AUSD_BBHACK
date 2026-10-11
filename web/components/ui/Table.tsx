/**
 * Table: a dense, column-driven data table (purchases, findings, catalog, allocations, …).
 *
 * Each column says how to render one cell from a row, so pages describe *what* to show and the
 * table handles the terminal styling: sticky header, hairline row borders, right-aligned
 * numbers. Rows come from the caller in display order; `empty` is shown when there are none.
 */

import type { ReactNode } from "react";

import { cx } from "@/lib/ui";

type Align = "left" | "right" | "center";

export interface Column<Row> {
  key: string;
  header: ReactNode;
  render: (row: Row) => ReactNode;
  /** Use "right" for amounts so decimal points line up. */
  align?: Align;
  className?: string;
}

interface TableProps<Row> {
  columns: Column<Row>[];
  rows: Row[];
  rowKey: (row: Row, index: number) => string | number;
  empty?: ReactNode;
  /** Extra classes per row, e.g. dim skipped purchases. */
  rowClassName?: (row: Row) => string | undefined;
  /** Max body height in px before the table scrolls (header stays visible). */
  maxHeight?: number;
}

const ALIGN_CLASSES: Record<Align, string> = {
  left: "text-left",
  right: "text-right",
  center: "text-center",
};

export function Table<Row>({ columns, rows, rowKey, empty = "Nothing yet.", rowClassName, maxHeight }: TableProps<Row>) {
  if (rows.length === 0) {
    return <div className="px-3 py-4 text-center text-xs text-fg-dim">{empty}</div>;
  }
  return (
    <div className="overflow-auto" style={maxHeight === undefined ? undefined : { maxHeight }}>
      <table className="w-full border-collapse text-xs">
        <thead className="sticky top-0 z-10 bg-surface">
          <tr className="border-b border-line">
            {columns.map((column) => (
              <th
                key={column.key}
                scope="col"
                className={cx(
                  "whitespace-nowrap px-2 py-1 text-[10px] font-semibold uppercase tracking-wider text-fg-dim",
                  ALIGN_CLASSES[column.align ?? "left"],
                )}
              >
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr
              key={rowKey(row, index)}
              className={cx("border-b border-line/60 hover:bg-surface-2", rowClassName?.(row))}
            >
              {columns.map((column) => (
                <td
                  key={column.key}
                  className={cx("px-2 py-1 align-top", ALIGN_CLASSES[column.align ?? "left"], column.className)}
                >
                  {column.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
