<?php

/**
 * Por que esta fixture existe
 * ---------------------------
 *
 * A destruição que o corpus real **não** exercita, no dialeto MySQL e dentro de
 * `up()`: `DROP COLUMN` e `MODIFY ... NOT NULL`. Sem esta fixture, um
 * `DROP COLUMN` do consumidor classificado como `safe` passaria a suíte inteira.
 */

declare(strict_types=1);

namespace Migrations;

use Doctrine\DBAL\Schema\Schema;
use Doctrine\Migrations\AbstractMigration;

final class Version20250101120200 extends AbstractMigration
{
    public function getDescription(): string
    {
        return 'Remove a coluna de liquidacao e exige o codigo da transportadora';
    }

    public function up(Schema $schema): void
    {
        $this->addSql('ALTER TABLE freight_note DROP COLUMN settled');
        $this->addSql('ALTER TABLE freight_note MODIFY carrier_code VARCHAR(40) NOT NULL');
    }

    public function down(Schema $schema): void
    {
        $this->addSql('ALTER TABLE freight_note MODIFY carrier_code VARCHAR(40) NULL');
        $this->addSql('ALTER TABLE freight_note ADD COLUMN settled TINYINT(1) NULL');
    }
}
