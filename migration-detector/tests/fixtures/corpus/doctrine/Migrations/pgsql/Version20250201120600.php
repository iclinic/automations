<?php

/**
 * Por que esta fixture existe
 * ---------------------------
 *
 * Duas classes de migração no mesmo arquivo, cada uma com o seu `up()`. Sem
 * resolver escopo não dá para dizer qual delas o Doctrine executa, e as duas
 * fazem coisas diferentes — uma acrescenta coluna, a outra dropa a tabela.
 * Escolher a primeira seria um palpite; a resposta é `unknown`.
 */

declare(strict_types=1);

namespace Migrations;

use Doctrine\DBAL\Schema\Schema;
use Doctrine\Migrations\AbstractMigration;

final class Version20250201120600 extends AbstractMigration
{
    public function up(Schema $schema): void
    {
        $this->addSql('ALTER TABLE stock."freight_note" ADD COLUMN settled boolean NULL');
    }

    public function down(Schema $schema): void
    {
        $this->addSql('ALTER TABLE stock."freight_note" DROP COLUMN settled');
    }
}

final class Version20250201120600Legacy extends AbstractMigration
{
    public function up(Schema $schema): void
    {
        $this->addSql('DROP TABLE stock."freight_note"');
    }

    public function down(Schema $schema): void
    {
        $this->addSql('CREATE TABLE stock."freight_note" ("id" SERIAL PRIMARY KEY)');
    }
}
