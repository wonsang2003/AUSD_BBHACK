/**
 * `/fund` — connect a wallet, get test AUSD, set caps and open the budget (one permit, or 7
 * daily vouchers in "Maximum safety" mode), pause / revoke, top-up QR. See
 * `components/fund/FundPage.tsx`; data comes from the backend API and the connected wallet.
 */

import { FundPage } from "@/components/fund/FundPage";

export default function Fund() {
  return <FundPage />;
}
