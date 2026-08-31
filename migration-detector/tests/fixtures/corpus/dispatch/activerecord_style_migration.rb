class DropMemoFromLedger < ActiveRecord::Migration[7.0]
  def change
    remove_column :ledger_entry, :memo
  end
end
