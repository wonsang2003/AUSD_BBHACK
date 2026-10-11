/**
 * DataTable: a dense terminal table — zebra rows (#0b0b0b / #111), 1px row separators, a #1a1a1a
 * header row with amber uppercase 11px labels that stays put while the body scrolls, fixed column
 * widths, and ellipsis for long text (pass `title` so the full text shows on hover).
 *
 * Props:
 *   columns       Column[]: { key, header, width?, align?, render(row), title?(row), headerTitle? }
 *                 width in px (omit for a flexible column); align "right" for every number.
 *   rows          the data
 *   rowKey        stable key per row
 *   empty         shown as ONE muted line when there are no rows
 *   maxHeight     px; the body scrolls beyond it with a sticky header
 *   rowClassName  extra classes per row (e.g. dim skipped rows)
 *   onRowClick    makes rows clickable
 *
 * Example:
 *   <DataTable rows={agents} rowKey={(a) => a.name} columns={[
 *     { key: "name", header: "Agent", width: 120, render: (a) => a.name },
 *     { key: "bal", header: "AUSD", width: 90, align: "right", render: (a) => fmtAusd(a.balance) },
 *   ]} />
 */

import type { ReactNode } from "react";

import { MutedLine } from "./InlineError";
import { cls } from "./tones";

export type ColumnAlign = "left" | "right" | "center";

export interface Column<Row> {
  key: string;
  header: ReactNode;
  /** Fixed width in px; omit for a flexible column (takes the remaining space). */
  width?: number;
  align?: ColumnAlign;
  render: (row: Row) => ReactNode;
  /** Tooltip for the cell, e.g. the full text of an ellipsized value. */
  title?: (row: Row) => string | undefined;
  /** Tooltip for the header. */
  headerTitle?: string;
}

interface DataTableProps<Row> {
  columns: Column<Row>[];
  rows: Row[];
  rowKey: (row: Row, index: number) => string | number;
  empty?: ReactNode;
  maxHeight?: number;
  rowClassName?: (row: Row) => string | undefined;
  onRowClick?: (row: Row) => void;
}

const FLEX_COLUMN_MIN_PX = 160;

function alignClass(align: ColumnAlign | undefined): string | undefined {
  if (align === "right") {
    return "t-right";
  }
  if (align === "center") {
    return "t-center";
  }
  return undefined;
}

/** Minimum table width: fixed widths plus a minimum for each flexible column. */
function minTableWidth<Row>(columns: Column<Row>[]): number {
  return columns.reduce((sum, column) => sum + (column.width ?? FLEX_COLUMN_MIN_PX), 0);
}

export function DataTable<Row>({
  columns,
  rows,
  rowKey,
  empty = "No rows.",
  maxHeight,
  rowClassName,
  onRowClick,
}: DataTableProps<Row>) {
  if (rows.length === 0) {
    return <MutedLine>{empty}</MutedLine>;
  }
  return (
    <div className="t-table-wrap" style={maxHeight ? { maxHeight } : undefined}>
      <table className="t-table" style={{ minWidth: minTableWidth(columns) }}>
        <colgroup>
          {columns.map((column) => (
            <col key={column.key} style={column.width ? { width: column.width } : undefined} />
          ))}
        </colgroup>
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column.key} className={alignClass(column.align)} title={column.headerTitle}>
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr
              key={rowKey(row, index)}
              className={cls(onRowClick && "t-row--click", rowClassName?.(row))}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
            >
              {columns.map((column) => (
                <td key={column.key} className={alignClass(column.align)} title={column.title?.(row)}>
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
