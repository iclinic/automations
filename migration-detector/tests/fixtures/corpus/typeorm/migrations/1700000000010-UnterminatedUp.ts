import {MigrationInterface, QueryRunner} from "typeorm";

export class UnterminatedUp1700000000010 implements MigrationInterface {

    public async up(queryRunner: QueryRunner): Promise<any> {
        await queryRunner.query(`ALTER TABLE "schedule" DROP COLUMN "room"`);
